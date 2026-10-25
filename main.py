"""
Telegram Book Studio — improved main application.

What this version adds:
- A persistent Telegram reply-keyboard menu.
- A real fresh-book-idea workflow (the old /newbook only saved user input).
- Cross-session idea history and anti-repetition checks.
- Inline buttons to save a generated concept as a book project.
- Natural-text shortcuts such as "new book".
- Guided prompts for the existing outline/write/read/review/revise/export commands.
- KDP publishing checklist, safer error handling, and preserved existing commands.

This file expects the repository's existing config.py, storage.py, engine.py,
exporter.py and requirements.txt. It does not require another paid API.
"""

from __future__ import annotations

import asyncio
import difflib
import json
import logging
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputFile,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import config
import exporter
import storage
from engine import WritingEngine, WritingEngineError


# ---------------------------------------------------------------------------
# Logging and limits
# ---------------------------------------------------------------------------

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("telegram_book_studio")
logging.getLogger("httpx").setLevel(logging.WARNING)

MAX_TELEGRAM_MESSAGE = 3900
SUPPORTED_EXPORT_FORMATS = {"txt", "docx", "epub", "pdf"}
IDEAS_PER_BATCH = 5
MAX_IDEA_HISTORY_PER_USER = 60
IDEA_HISTORY_PATH = config.DATA_DIR / "idea_history.json"
_IDEA_HISTORY_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# Persistent Telegram menu
# ---------------------------------------------------------------------------

BTN_IDEAS = "🌟 Generate New Book Ideas"
BTN_MANUAL = "➕ Create Book Manually"
BTN_BOOKS = "📚 My Books"
BTN_OUTLINE = "🗺️ Generate Outline"
BTN_WRITE = "✍️ Write Chapter"
BTN_CHAPTER = "📖 Read Chapter"
BTN_REVIEW = "📝 Review Chapter"
BTN_REVISE = "🛠️ Revise Chapter"
BTN_STATUS = "📊 Book Statistics"
BTN_EXPORT = "📦 Export Manuscript"
BTN_KDP = "🧾 KDP Checklist"
BTN_HELP = "❓ Help"
BTN_CANCEL = "❌ Cancel"

MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [
        [BTN_IDEAS, BTN_MANUAL],
        [BTN_BOOKS, BTN_OUTLINE],
        [BTN_WRITE, BTN_CHAPTER],
        [BTN_REVIEW, BTN_REVISE],
        [BTN_STATUS, BTN_EXPORT],
        [BTN_KDP, BTN_HELP],
        [BTN_CANCEL],
    ],
    resize_keyboard=True,
    is_persistent=True,
    input_field_placeholder="Choose an action or type ‘new book’…",
)


class _ContextWithArgs:
    """Expose command arguments while forwarding all other context fields."""

    def __init__(self, original: Any, args: list[str]) -> None:
        self._original = original
        self.args = args

    def __getattr__(self, name: str) -> Any:
        return getattr(self._original, name)


MENU_ACTIONS: dict[str, tuple[str, str, Callable[..., Any] | None]] = {
    BTN_IDEAS: (
        "ideas",
        "I'll generate five new concepts and check them against your past ideas.",
        None,
    ),
    BTN_MANUAL: (
        "manual_newbook",
        "Send the book details in this format:\n\n"
        "Title | Genre | Audience | Premise\n\n"
        "Example:\nThe Glass Orchard | Gothic mystery | Adult readers | "
        "A village botanist discovers that every tree planted over a grave "
        "grows a fruit containing one memory of the dead.",
        None,
    ),
    BTN_BOOKS: ("books", "", None),
    BTN_OUTLINE: (
        "outline",
        "Which book and how many chapters? Send:\nBOOK_ID | CHAPTER_COUNT\n\nExample: 1 | 12",
        None,
    ),
    BTN_WRITE: (
        "write",
        "Which chapter should I write? Send:\nBOOK_ID CHAPTER_NUMBER\n\nExample: 1 1",
        None,
    ),
    BTN_CHAPTER: (
        "chapter",
        "Which saved chapter should I open? Send:\nBOOK_ID CHAPTER_NUMBER\n\nExample: 1 1",
        None,
    ),
    BTN_REVIEW: (
        "review",
        "Which saved chapter should I review? Send:\nBOOK_ID CHAPTER_NUMBER\n\nExample: 1 1",
        None,
    ),
    BTN_REVISE: (
        "revise",
        "Send the chapter and what you want improved:\n"
        "BOOK_ID CHAPTER_NUMBER | REVISION INSTRUCTIONS\n\n"
        "Example: 1 1 | Strengthen the opening and remove repetitive dialogue.",
        None,
    ),
    BTN_STATUS: (
        "status",
        "Which book's statistics do you want? Send its BOOK_ID.\nExample: 1",
        None,
    ),
    BTN_EXPORT: (
        "export",
        "Which book and format? Send:\nBOOK_ID FORMAT\n\n"
        "Formats: docx, epub, pdf, txt\nExample: 1 docx",
        None,
    ),
    BTN_KDP: ("kdpcheck", "", None),
    BTN_HELP: ("help", "", None),
    BTN_CANCEL: ("cancel", "", None),
}

COMMAND_BY_MENU_STATE: dict[str, Callable[..., Any]] = {}


# ---------------------------------------------------------------------------
# Common helpers
# ---------------------------------------------------------------------------

def get_user_id(update: Update) -> int:
    """Return the Telegram ID for the current user."""
    if update.effective_user is None:
        raise ValueError("Could not identify the Telegram user.")
    return update.effective_user.id


def get_engine() -> WritingEngine:
    """Create a writing engine for one operation."""
    return WritingEngine()


def parse_pipe_arguments(text: str, expected_parts: int) -> list[str]:
    """Parse N non-empty fields separated by |, leaving later pipes in final field."""
    parts = [part.strip() for part in text.split("|", expected_parts - 1)]
    if len(parts) != expected_parts:
        raise ValueError(f"Expected {expected_parts} fields separated by |.")
    if any(not part for part in parts):
        raise ValueError("All required fields must contain a value.")
    return parts


def parse_positive_integer(value: str, field_name: str) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be a whole number.") from exc
    if number < 1:
        raise ValueError(f"{field_name} must be at least 1.")
    return number


def get_book_or_raise(book_id: int, user_id: int) -> dict:
    book = storage.get_book(book_id, user_id)
    if book is None:
        raise ValueError("Book not found. Tap 📚 My Books to see your projects.")
    return book


def get_outline_or_raise(book_id: int, user_id: int) -> dict:
    outline = storage.get_outline(book_id, user_id)
    if not outline or not outline.get("chapters"):
        raise ValueError(
            "This book has no saved outline yet. Generate one from the menu first."
        )
    return outline


def get_chapter_or_raise(book_id: int, user_id: int, chapter_number: int) -> dict:
    chapter = storage.get_chapter(book_id, user_id, chapter_number)
    if chapter is None:
        raise ValueError("Saved chapter not found. Check the book ID and chapter number.")
    return chapter


def truncate_text(text: str, limit: int = MAX_TELEGRAM_MESSAGE) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 100] + "\n\n[Message shortened. Use Export Manuscript for the complete text.]"


def split_message(text: str, limit: int = MAX_TELEGRAM_MESSAGE) -> list[str]:
    chunks: list[str] = []
    while len(text) > limit:
        split_at = text.rfind("\n", 0, limit)
        if split_at < limit // 2:
            split_at = limit
        chunks.append(text[:split_at])
        text = text[split_at:].lstrip()
    if text:
        chunks.append(text)
    return chunks


async def send_long_message(update: Update, text: str) -> None:
    if update.effective_message is None:
        return
    for chunk in split_message(text):
        await update.effective_message.reply_text(chunk, reply_markup=MAIN_KEYBOARD)


async def send_typing_action(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> None:
    try:
        await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    except Exception:
        logger.debug("Could not send typing indicator.", exc_info=True)


async def _reply(update: Update, text: str) -> None:
    if update.effective_message is not None:
        await update.effective_message.reply_text(text, reply_markup=MAIN_KEYBOARD)


# ---------------------------------------------------------------------------
# Durable anti-repetition memory for generated book concepts
# ---------------------------------------------------------------------------

def _load_idea_history() -> dict[str, list[dict[str, Any]]]:
    """Read local idea memory. DATA_DIR should be on a Railway persistent volume."""
    with _IDEA_HISTORY_LOCK:
        try:
            if not IDEA_HISTORY_PATH.exists():
                return {}
            parsed = json.loads(IDEA_HISTORY_PATH.read_text(encoding="utf-8"))
            if not isinstance(parsed, dict):
                return {}
            result: dict[str, list[dict[str, Any]]] = {}
            for key, value in parsed.items():
                if isinstance(value, list):
                    result[str(key)] = [item for item in value if isinstance(item, dict)]
            return result
        except (OSError, json.JSONDecodeError):
            logger.exception("Could not read concept history; starting with empty memory.")
            return {}


def _save_idea_history(history: dict[str, list[dict[str, Any]]]) -> None:
    """Write idea memory atomically to reduce the chance of a partial JSON file."""
    with _IDEA_HISTORY_LOCK:
        try:
            config.DATA_DIR.mkdir(parents=True, exist_ok=True)
            temporary = IDEA_HISTORY_PATH.with_suffix(".tmp")
            temporary.write_text(
                json.dumps(history, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            temporary.replace(IDEA_HISTORY_PATH)
        except OSError:
            logger.exception("Could not persist generated concept history.")


def _concept_history_for_user(user_id: int) -> list[dict[str, Any]]:
    history = _load_idea_history()
    entries = history.get(str(user_id), [])
    # Merge manually-created/saved books into exclusion memory too.
    try:
        books = storage.list_books(user_id)
    except Exception:
        logger.exception("Could not load saved books for idea de-duplication.")
        books = []
    known: dict[str, dict[str, Any]] = {}
    for item in entries:
        known[str(item.get("title", "")).casefold()] = item
    for book in books:
        title = str(book.get("title", "")).strip()
        if title:
            known[title.casefold()] = {
                "title": title,
                "genre": str(book.get("genre", "")),
                "audience": str(book.get("audience", "")),
                "premise": str(book.get("premise", "")),
                "source": "saved book",
            }
    return list(known.values())[-MAX_IDEA_HISTORY_PER_USER:]


def _remember_ideas(user_id: int, ideas: list[dict[str, Any]], source: str) -> None:
    history = _load_idea_history()
    key = str(user_id)
    entries = history.get(key, [])
    known_titles = {str(item.get("title", "")).casefold() for item in entries}
    for idea in ideas:
        title = str(idea.get("title", "")).strip()
        if not title:
            continue
        record = {
            "title": title,
            "genre": str(idea.get("genre", "")),
            "audience": str(idea.get("audience", "")),
            "premise": str(idea.get("premise", "")),
            "hook": str(idea.get("hook", "")),
            "source": source,
            "recorded_at": int(time.time()),
        }
        if title.casefold() in known_titles:
            # Refresh with the newest full premise rather than duplicating title.
            entries = [old for old in entries if str(old.get("title", "")).casefold() != title.casefold()]
        entries.append(record)
        known_titles.add(title.casefold())
    history[key] = entries[-MAX_IDEA_HISTORY_PER_USER:]
    _save_idea_history(history)


def _important_words(text: str) -> set[str]:
    ignored = {
        "about", "after", "again", "against", "along", "among", "another", "before",
        "being", "between", "could", "every", "from", "have", "into", "just", "more",
        "most", "other", "over", "same", "some", "that", "their", "there", "these",
        "thing", "this", "those", "through", "under", "until", "very", "when", "where",
        "which", "while", "with", "would", "your", "they", "them", "then", "than", "were",
        "will", "what", "who", "whom", "book", "story", "novel", "woman", "man", "girl",
        "boy", "people", "must", "finds", "discovers", "learns", "afterward", "secret",
    }
    tokens = set(re.findall(r"[a-z]{4,}", text.casefold()))
    return tokens - ignored


def _is_too_similar(candidate: dict[str, Any], previous: list[dict[str, Any]]) -> bool:
    """Catch near-identical titles/pitches in code, not just by prompt instruction."""
    title = re.sub(r"[^a-z0-9]+", " ", str(candidate.get("title", "")).casefold()).strip()
    premise = str(candidate.get("premise", ""))
    candidate_text = f"{title} {premise}"
    candidate_words = _important_words(candidate_text)
    for old in previous:
        old_title = re.sub(r"[^a-z0-9]+", " ", str(old.get("title", "")).casefold()).strip()
        old_text = f"{old_title} {old.get('premise', '')}"
        if title and title == old_title:
            return True
        title_similarity = difflib.SequenceMatcher(None, title, old_title).ratio() if title and old_title else 0
        if title_similarity >= 0.88:
            return True
        old_words = _important_words(old_text)
        union = candidate_words | old_words
        overlap = len(candidate_words & old_words) / len(union) if union else 0
        if overlap >= 0.62:
            return True
    return False


def _extract_json_object(text: str) -> dict[str, Any]:
    """Parse an AI JSON object, accepting common Markdown-fence mistakes."""
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        result = json.loads(cleaned)
        if isinstance(result, dict):
            return result
    except json.JSONDecodeError:
        pass
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start >= 0 and end > start:
        try:
            result = json.loads(cleaned[start : end + 1])
            if isinstance(result, dict):
                return result
        except json.JSONDecodeError:
            pass
    raise WritingEngineError("The AI returned an invalid concept response. Please generate ideas again.")


def _normalise_idea(raw: dict[str, Any]) -> dict[str, str] | None:
    fields = ("title", "genre", "audience", "premise", "hook", "differentiator")
    idea: dict[str, str] = {}
    for field in fields:
        value = raw.get(field, "")
        idea[field] = value.strip() if isinstance(value, str) else str(value).strip()
    if not idea["title"] or len(idea["premise"]) < 35 or not idea["genre"]:
        return None
    if not idea["audience"]:
        idea["audience"] = "Adult readers"
    if not idea["hook"]:
        idea["hook"] = "A strong central mystery or conflict drives the plot."
    if not idea["differentiator"]:
        idea["differentiator"] = "The concept must be developed through distinctive characters and specific stakes."
    return idea


async def generate_fresh_book_ideas(
    user_id: int,
    user_direction: str = "",
) -> list[dict[str, str]]:
    """Generate differentiated concepts and reject near-duplicates against saved history."""
    engine = get_engine()
    previous = _concept_history_for_user(user_id)
    recent_memory = [
        {
            "title": item.get("title", ""),
            "genre": item.get("genre", ""),
            "premise": str(item.get("premise", ""))[:360],
            "hook": str(item.get("hook", ""))[:180],
        }
        for item in previous[-35:]
    ]

    system_prompt = """
You are an exceptional commercial-fiction development editor and original-story generator.
Your job is to create DISTINCT book concepts that can be developed into strong, readable
Amazon KDP fiction—not generic writing exercises or recycled tropes.

QUALITY RULES:
1. Make each concept meaningfully different in premise, conflict, setting, protagonist,
   emotional engine, and central twist. Changing only names or locations is not enough.
2. Avoid generic hooks such as a chosen one discovering hidden powers, a billionaire romance,
   a detective with a dark past, a village with a secret, or a prophecy unless the execution
   has a genuinely unusual and specific mechanism.
3. Use specific cause-and-effect: the protagonist wants something concrete, a surprising
   problem blocks it, and each attempted solution makes matters worse.
4. The premise must support a complete novel, not only a short scene or a one-line gimmick.
5. Prefer ideas whose emotional promise, target readership, and genre are clear.
6. Do not imitate or continue any known franchise, living author's signature style, or
   copyrighted characters. Do not use real authors as style targets.
7. Do not repeat or closely paraphrase any idea in the excluded-history list.
8. Avoid inventing sales data or claiming that an idea is guaranteed to sell. There is no
   reliable way to guarantee a market outcome from a premise alone.
9. Generate exactly five concepts. Give each a different core engine and avoid using the
   same genre more than twice unless the user explicitly asks for one genre.
10. Write a concise but vivid premise that is specific enough for an outline writer to use.

Return one JSON object only, with this exact structure:
{
  "ideas": [
    {
      "title": "A distinctive working title",
      "genre": "Specific genre/subgenre",
      "audience": "Specific intended readership",
      "premise": "A 2-4 sentence original plot premise with protagonist, goal, obstacle, and stakes",
      "hook": "One sentence explaining the reader-facing hook",
      "differentiator": "What makes this concept different from familiar genre patterns"
    }
  ]
}
""".strip()

    direction = user_direction.strip() or "No genre or theme specified; deliberately diversify the batch."
    exclusion_json = json.dumps(recent_memory, ensure_ascii=False)
    variation_id = uuid.uuid4().hex[:10]
    user_prompt = f"""
USER'S DIRECTION:
{direction}

ANTI-REPETITION HISTORY (these are exclusions, not inspiration; do not reuse them):
{exclusion_json}

BATCH VARIATION ID: {variation_id}
Use this only to push toward fresh creative choices; never include it in a title or premise.

Generate exactly five distinct concepts. Prioritize novelty, clear story engines,
commercial genre clarity, and emotional consequence. Return JSON only.
""".strip()

    accepted: list[dict[str, str]] = []
    excluded_this_attempt: list[dict[str, Any]] = list(previous)
    last_error = "The model did not return enough distinct concepts."

    # Retry once if the first response is malformed or heavily repetitive.
    for attempt in range(2):
        prompt = user_prompt
        if attempt:
            prompt += "\n\nRETRY: The previous output was unusable or too similar to earlier work. " \
                      "Use a substantially different premise architecture, setting, protagonist, " \
                      "and conflict. Do not salvage the same plot by renaming characters."
        raw = await engine._request(
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt},
            ],
            temperature=0.95 if attempt == 0 else 1.0,
            max_tokens=6500,
        )
        try:
            parsed = _extract_json_object(raw)
        except WritingEngineError as exc:
            last_error = str(exc)
            continue

        raw_ideas = parsed.get("ideas")
        if not isinstance(raw_ideas, list):
            last_error = "The AI response did not include an ideas list."
            continue

        accepted = []
        for raw_idea in raw_ideas:
            if not isinstance(raw_idea, dict):
                continue
            idea = _normalise_idea(raw_idea)
            if idea is None:
                continue
            if _is_too_similar(idea, excluded_this_attempt + accepted):
                continue
            accepted.append(idea)
            if len(accepted) >= IDEAS_PER_BATCH:
                break

        if len(accepted) >= 3:
            break
        excluded_this_attempt.extend(accepted)
        last_error = "The AI returned too few genuinely different ideas."

    if len(accepted) < 3:
        raise WritingEngineError(
            f"I couldn't produce enough distinct concepts after two attempts. {last_error} "
            "Try adding a genre or a different direction, then generate again."
        )

    _remember_ideas(user_id, accepted, source="generated concept")
    return accepted


# ---------------------------------------------------------------------------
# Start, help and book idea workflow
# ---------------------------------------------------------------------------

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = (
        "📚 TELEGRAM BOOK STUDIO\n\n"
        "Let's develop original books, keep the story consistent, and prepare "
        "manuscripts for your final review. Use the menu below—no need to memorise commands.\n\n"
        "START HERE\n"
        "🌟 Generate New Book Ideas creates fresh concepts and checks them against your saved idea history.\n"
        "➕ Create Book Manually saves a concept you already have.\n"
        "🗺️ Generate Outline plans the full story.\n"
        "✍️ Write Chapter drafts from the saved outline and story context.\n"
        "📝 Review Chapter and 🛠️ Revise Chapter help improve the manuscript.\n"
        "📦 Export Manuscript creates a file for your own final checks.\n\n"
        "IMPORTANT: AI-generated content still needs careful human review. KDP may require you "
        "to disclose AI-generated text, images, or translations when publishing. See the KDP checklist.\n\n"
        "Your persistent menu is below."
    )
    if update.effective_message:
        await update.effective_message.reply_text(message, reply_markup=MAIN_KEYBOARD)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await start_command(update, context)


async def ideas_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Generate a batch of new ideas, optionally guided by /ideas <genre or direction>."""
    if update.effective_message is None:
        return
    raw_args = getattr(context, "args", None) or []
    direction = " ".join(raw_args).strip()
    user_id = get_user_id(update)
    status_message = await update.effective_message.reply_text(
        "🧠 Developing a fresh batch of book concepts…\n\n"
        "I'm comparing the new concepts with your saved projects and recent idea history. "
        "This can take a little while.",
        reply_markup=MAIN_KEYBOARD,
    )
    try:
        ideas = await generate_fresh_book_ideas(user_id, direction)
    except (WritingEngineError, ValueError) as exc:
        await status_message.edit_text(
            f"❌ I couldn't generate book ideas this time.\n\n{exc}\n\n"
            "Your existing projects have not been changed. Try again or specify a genre with /ideas mystery.",
        )
        return
    except Exception:
        logger.exception("Book idea generation failed.")
        await status_message.edit_text(
            "❌ An unexpected error occurred while generating ideas. "
            "Your projects have not been changed. Check Railway logs, then retry."
        )
        return

    batch_id = uuid.uuid4().hex[:8]
    batches = context.user_data.setdefault("idea_batches", {})
    batches[batch_id] = ideas
    # Keep recent batches only; candidates themselves are in the durable idea history.
    while len(batches) > 4:
        batches.pop(next(iter(batches)))

    lines = ["🌟 FRESH BOOK CONCEPTS", ""]
    for index, idea in enumerate(ideas, start=1):
        lines.extend(
            [
                f"{index}. {idea['title']}",
                f"Genre: {idea['genre']} | Audience: {idea['audience']}",
                f"Premise: {idea['premise']}",
                f"Reader hook: {idea['hook']}",
                f"Why it stands out: {idea['differentiator']}",
                "",
            ]
        )
    lines.append("Tap a button below to turn a concept into a saved book project.")

    rows = [
        [InlineKeyboardButton(f"✅ Save Idea {i}", callback_data=f"idea:select:{batch_id}:{i - 1}")]
        for i in range(1, len(ideas) + 1)
    ]
    rows.append([InlineKeyboardButton("♻️ Generate Different Ideas", callback_data=f"idea:again:{batch_id}")])

    try:
        await status_message.edit_text(
            truncate_text("\n".join(lines), 3900),
            reply_markup=InlineKeyboardMarkup(rows),
        )
    except Exception:
        logger.exception("Could not edit idea-generation status message; sending a new result message.")
        await update.effective_message.reply_text(
            truncate_text("\n".join(lines), 3900),
            reply_markup=InlineKeyboardMarkup(rows),
        )


async def idea_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or not query.data:
        return
    await query.answer()
    parts = query.data.split(":")
    if len(parts) < 3 or parts[0] != "idea":
        return

    action = parts[1]
    batch_id = parts[2]
    batches = context.user_data.get("idea_batches", {})
    ideas = batches.get(batch_id)
    if not isinstance(ideas, list):
        await query.message.reply_text(
            "That idea batch has expired from the current session. Tap 🌟 Generate New Book Ideas to create a fresh batch.",
            reply_markup=MAIN_KEYBOARD,
        )
        return

    if action == "again":
        # Generated candidates were already placed in durable history, so the next batch excludes them.
        await query.message.reply_text("♻️ Creating a different batch…", reply_markup=MAIN_KEYBOARD)
        await ideas_command(update, context)
        return

    if action != "select" or len(parts) != 4:
        return
    try:
        index = int(parts[3])
        idea = ideas[index]
    except (ValueError, IndexError, TypeError):
        await query.message.reply_text("That idea button is no longer valid. Generate a new batch.", reply_markup=MAIN_KEYBOARD)
        return

    try:
        book = storage.create_book(
            user_id=get_user_id(update),
            title=idea["title"],
            genre=idea["genre"],
            audience=idea["audience"],
            premise=idea["premise"],
        )
    except Exception:
        logger.exception("Could not save selected generated concept.")
        await query.message.reply_text(
            "I couldn't save that project. Your idea has not been removed from history. Please try again.",
            reply_markup=MAIN_KEYBOARD,
        )
        return

    try:
        await query.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    await query.message.reply_text(
        f"✅ BOOK PROJECT CREATED\n\n"
        f"ID: {book['id']}\n"
        f"Title: {book['title']}\n"
        f"Genre: {book['genre']}\n"
        f"Audience: {book['audience']}\n\n"
        f"PREMISE\n{book['premise']}\n\n"
        "NEXT STEP\nTap 🗺️ Generate Outline and enter:\n"
        f"{book['id']} | 12\n\n"
        "The project is saved. No outline or chapter will be overwritten by choosing another idea.",
        reply_markup=MAIN_KEYBOARD,
    )


async def _create_manual_book(update: Update, raw: str) -> None:
    title, genre, audience, premise = parse_pipe_arguments(raw, 4)
    book = storage.create_book(
        user_id=get_user_id(update),
        title=title,
        genre=genre,
        audience=audience,
        premise=premise,
    )
    await _reply(
        update,
        f"✅ Book project created.\n\nID: {book['id']}\nTitle: {book['title']}\n"
        f"Genre: {book['genre']}\nAudience: {book['audience']}\n\n"
        "Next: tap 🗺️ Generate Outline and send:\n"
        f"{book['id']} | 12",
    )


async def newbook_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Backwards-compatible command: no args generates ideas; supplied fields create manually."""
    if update.effective_message is None:
        return
    raw = " ".join(getattr(context, "args", None) or []).strip()
    if not raw:
        await ideas_command(update, context)
        return
    try:
        await _create_manual_book(update, raw)
    except ValueError as exc:
        await _reply(
            update,
            f"Could not create book: {exc}\n\n"
            "Use 🌟 Generate New Book Ideas, or send manual details as:\n"
            "Title | Genre | Audience | Premise",
        )


async def books_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_message is None:
        return
    user_id = get_user_id(update)
    books = storage.list_books(user_id)
    if not books:
        await _reply(update, "You have no saved books yet. Tap 🌟 Generate New Book Ideas or ➕ Create Book Manually.")
        return
    lines = ["📚 YOUR BOOKS", ""]
    for book in books:
        stats = storage.get_book_statistics(book["id"], user_id)
        if stats is None:
            continue
        lines.extend(
            [
                f"ID: {book['id']} | {book['title']}",
                f"Genre: {book['genre']} | Status: {book['status']}",
                f"Chapters: {stats['chapter_count']} | Words: {stats['total_words']:,}",
                "",
            ]
        )
    await send_long_message(update, "\n".join(lines))


# ---------------------------------------------------------------------------
# Outline generation
# ---------------------------------------------------------------------------

async def outline_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_message is None:
        return
    raw = " ".join(getattr(context, "args", None) or []).strip()
    if not raw:
        await _reply(update, "Send BOOK_ID | CHAPTER_COUNT\nExample: 1 | 12")
        return
    try:
        book_part, count_part = parse_pipe_arguments(raw, 2)
        book_id = parse_positive_integer(book_part, "Book ID")
        chapter_count = parse_positive_integer(count_part, "Chapter count")
        user_id = get_user_id(update)
        book = get_book_or_raise(book_id, user_id)
        if chapter_count > config.MAX_CHAPTERS:
            raise ValueError(f"Chapter count cannot exceed {config.MAX_CHAPTERS}.")
        await _reply(
            update,
            "🧠 Building the story architecture: character goals, conflict escalation, causal chapter progression, and a planned ending…",
        )
        engine = get_engine()
        outline = await engine.generate_outline(
            title=book["title"],
            genre=book["genre"],
            audience=book["audience"],
            premise=book["premise"],
            chapter_count=chapter_count,
        )
        if not isinstance(outline.get("chapters"), list) or len(outline["chapters"]) != chapter_count:
            raise WritingEngineError("The generated outline did not contain the requested number of chapters. Try again.")
        if not storage.save_outline(book_id, user_id, outline):
            raise ValueError("The outline could not be saved.")
        story_bible = {
            "characters": outline.get("characters", []),
            "worldbuilding": outline.get("worldbuilding", []),
            "themes": outline.get("themes", []),
            "continuity_facts": [],
            "unresolved_threads": [],
        }
        storage.save_story_bible(book_id, user_id, story_bible)
        chapter_lines = [
            f"{chapter.get('number', '?')}. {chapter.get('title', 'Untitled')}\n   {chapter.get('summary', '')}"
            for chapter in outline["chapters"]
        ]
        response = (
            f"✅ OUTLINE SAVED\n\nBook: {book['title']}\nBook ID: {book_id}\n"
            f"Chapters: {len(outline['chapters'])}\n\nSYNOPSIS\n{outline.get('synopsis', '')}\n\n"
            "CHAPTER PLAN\n\n" + "\n\n".join(chapter_lines) +
            f"\n\nNext: tap ✍️ Write Chapter and send {book_id} 1."
        )
        await send_long_message(update, response)
    except (ValueError, WritingEngineError) as exc:
        await _reply(update, f"❌ Outline generation failed:\n{exc}")
    except Exception:
        logger.exception("Outline generation failed.")
        await _reply(update, "An unexpected error occurred while creating the outline. Your saved project has been preserved. Check Railway logs before retrying.")


# ---------------------------------------------------------------------------
# Chapter writing
# ---------------------------------------------------------------------------

async def write_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_message is None:
        return
    args = getattr(context, "args", None) or []
    if len(args) != 2:
        await _reply(update, "Send BOOK_ID CHAPTER_NUMBER\nExample: 1 1")
        return
    try:
        book_id = parse_positive_integer(args[0], "Book ID")
        chapter_number = parse_positive_integer(args[1], "Chapter number")
        user_id = get_user_id(update)
        book = get_book_or_raise(book_id, user_id)
        outline = get_outline_or_raise(book_id, user_id)
        chapter_plan = next((c for c in outline["chapters"] if c.get("number") == chapter_number), None)
        if chapter_plan is None:
            raise ValueError("That chapter number is not in the saved outline.")
        previous_chapters = storage.list_chapters(book_id, user_id) or []
        previous_chapters = [c for c in previous_chapters if c["chapter_number"] < chapter_number]
        story_bible = storage.get_story_bible(book_id, user_id) or {}
        await _reply(update, f"✍️ Writing Chapter {chapter_number} of ‘{book['title']}’ using the saved outline, earlier chapters, and story bible…")
        result = await get_engine().write_chapter(
            book=book,
            outline=outline,
            chapter_number=chapter_number,
            previous_chapters=previous_chapters,
            story_bible=story_bible,
        )
        saved = storage.save_chapter(
            book_id=book_id,
            user_id=user_id,
            chapter_number=chapter_number,
            title=result["title"],
            content=result["content"],
            summary=result["summary"],
            status="draft",
            revision_note="AI-generated chapter",
        )
        old_facts = story_bible.get("continuity_facts", [])
        new_facts = result.get("continuity_facts", [])
        story_bible["continuity_facts"] = (old_facts + new_facts)[-100:]
        storage.save_story_bible(book_id, user_id, story_bible)
        storage.update_book_status(book_id, user_id, "writing")
        response = (
            f"✅ CHAPTER SAVED AS DRAFT\n\nBook: {book['title']}\nChapter: {chapter_number}\n"
            f"Title: {saved['title']}\nWords: {saved['word_count']:,}\n\n"
            f"SUMMARY\n{saved['summary']}\n\nCHAPTER\n\n{saved['content']}\n\n"
            "Next: use 📝 Review Chapter before deciding whether the chapter is ready."
        )
        await send_long_message(update, response)
    except (ValueError, WritingEngineError) as exc:
        await _reply(update, f"❌ Chapter generation failed:\n{exc}")
    except Exception:
        logger.exception("Chapter generation failed.")
        await _reply(update, "An unexpected error occurred while writing the chapter. Your previously saved version is preserved in revision history. Check Railway logs before retrying.")


# ---------------------------------------------------------------------------
# Read, review and revise chapters
# ---------------------------------------------------------------------------

async def chapter_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_message is None:
        return
    args = getattr(context, "args", None) or []
    if len(args) != 2:
        await _reply(update, "Send BOOK_ID CHAPTER_NUMBER\nExample: 1 1")
        return
    try:
        book_id = parse_positive_integer(args[0], "Book ID")
        chapter_number = parse_positive_integer(args[1], "Chapter number")
        user_id = get_user_id(update)
        book = get_book_or_raise(book_id, user_id)
        chapter = get_chapter_or_raise(book_id, user_id, chapter_number)
        await send_long_message(
            update,
            f"📖 {book['title']}\nChapter {chapter_number}: {chapter['title']}\n"
            f"Status: {chapter['status']} | Words: {chapter['word_count']:,}\n\n{chapter['content']}",
        )
    except ValueError as exc:
        await _reply(update, str(exc))


async def review_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_message is None:
        return
    args = getattr(context, "args", None) or []
    if len(args) != 2:
        await _reply(update, "Send BOOK_ID CHAPTER_NUMBER\nExample: 1 1")
        return
    try:
        book_id = parse_positive_integer(args[0], "Book ID")
        chapter_number = parse_positive_integer(args[1], "Chapter number")
        user_id = get_user_id(update)
        book = get_book_or_raise(book_id, user_id)
        outline = get_outline_or_raise(book_id, user_id)
        chapter = get_chapter_or_raise(book_id, user_id, chapter_number)
        previous = storage.list_chapters(book_id, user_id) or []
        previous = [item for item in previous if item["chapter_number"] < chapter_number]
        await _reply(update, "🔎 Auditing plot progression, character logic, scene construction, dialogue, prose, pacing, repetition, genre fit, and continuity…")
        report = await get_engine().review_chapter(
            book=book,
            outline=outline,
            chapter_number=chapter_number,
            chapter_title=chapter["title"],
            content=chapter["content"],
            previous_chapters=previous,
        )
        lines = [
            "📝 EDITORIAL REPORT",
            "",
            f"Book: {book['title']}",
            f"Chapter: {chapter_number}",
            "",
            "OVERALL ASSESSMENT",
            str(report.get("overall_assessment", "No overall assessment returned.")),
            "",
            "STRENGTHS",
        ]
        strengths = report.get("strengths", [])
        lines.extend([f"• {item}" for item in strengths] if isinstance(strengths, list) else [])
        lines.extend(["", "ISSUES TO ADDRESS"])
        for issue in report.get("issues", []):
            if isinstance(issue, dict):
                lines.extend([
                    f"[{str(issue.get('severity', 'unspecified')).upper()}] {issue.get('problem', '')}",
                    f"Evidence: {issue.get('evidence', '')}",
                    f"Recommendation: {issue.get('recommendation', '')}",
                    "",
                ])
        lines.extend(["CONTINUITY WARNINGS"])
        lines.extend([f"• {item}" for item in report.get("continuity_warnings", [])])
        lines.extend(["", "REVISION PRIORITIES"])
        lines.extend([f"• {item}" for item in report.get("revision_priorities", [])])
        lines.extend([
            "",
            "To revise: tap 🛠️ Revise Chapter and send:",
            f"{book_id} {chapter_number} | Apply the report's highest-priority fixes without changing established plot facts.",
            "",
            "This report is editorial assistance, not proof that the chapter is publication-ready. Review it yourself.",
        ])
        await send_long_message(update, "\n".join(lines))
    except (ValueError, WritingEngineError) as exc:
        await _reply(update, f"Review failed:\n{exc}")
    except Exception:
        logger.exception("Chapter review failed.")
        await _reply(update, "An unexpected error occurred during review. Check Railway logs; the manuscript has not been replaced.")


async def revise_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_message is None:
        return
    raw = " ".join(getattr(context, "args", None) or []).strip()
    if "|" not in raw:
        await _reply(update, "Send BOOK_ID CHAPTER_NUMBER | REVISION INSTRUCTIONS\nExample: 1 1 | Strengthen the ending and remove repetitive dialogue.")
        return
    try:
        command_part, instructions = raw.split("|", 1)
        identifiers = command_part.strip().split()
        if len(identifiers) != 2:
            raise ValueError("Provide a book ID and chapter number before |.")
        book_id = parse_positive_integer(identifiers[0], "Book ID")
        chapter_number = parse_positive_integer(identifiers[1], "Chapter number")
        instructions = instructions.strip()
        if not instructions:
            raise ValueError("Provide revision instructions after |.")
        user_id = get_user_id(update)
        book = get_book_or_raise(book_id, user_id)
        outline = get_outline_or_raise(book_id, user_id)
        chapter = get_chapter_or_raise(book_id, user_id, chapter_number)
        await _reply(update, "🛠️ Revising the chapter. The previous saved version will remain in revision history.")
        result = await get_engine().revise_chapter(
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
            summary=result.get("summary") or chapter["summary"],
            status="draft",
            revision_note=instructions,
        )
        await send_long_message(
            update,
            f"✅ REVISED CHAPTER SAVED AS DRAFT\n\nBook: {book['title']}\n"
            f"Chapter: {chapter_number}\nTitle: {revised['title']}\nWords: {revised['word_count']:,}\n\n"
            f"{revised['content']}\n\nThe previous version is preserved in revision history. Review the changes before publishing.",
        )
    except (ValueError, WritingEngineError) as exc:
        await _reply(update, f"Revision failed:\n{exc}")
    except Exception:
        logger.exception("Chapter revision failed.")
        await _reply(update, "An unexpected error occurred during revision. The previous saved version should remain in revision history; check Railway logs.")


# ---------------------------------------------------------------------------
# Book statistics, export, KDP checklist
# ---------------------------------------------------------------------------

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_message is None:
        return
    args = getattr(context, "args", None) or []
    if len(args) != 1:
        await _reply(update, "Send the BOOK_ID. Example: 1")
        return
    try:
        book_id = parse_positive_integer(args[0], "Book ID")
        user_id = get_user_id(update)
        stats = storage.get_book_statistics(book_id, user_id)
        if stats is None:
            raise ValueError("Book not found. Tap 📚 My Books to see your projects.")
        chapters = storage.list_chapters(book_id, user_id) or []
        approved_words = sum(ch["word_count"] for ch in chapters if ch["status"] == "approved")
        await _reply(
            update,
            f"📊 BOOK STATUS\n\nTitle: {stats['title']}\nBook ID: {book_id}\n"
            f"Workflow: {stats['status']}\nSaved chapters: {stats['chapter_count']}\n"
            f"Approved chapters: {stats['approved_chapters']}\nTotal words: {stats['total_words']:,}\n"
            f"Words in approved chapters: {approved_words:,}\n\n"
            "Use Review Chapter to assess drafts. The status field alone is not a quality verdict.",
        )
    except ValueError as exc:
        await _reply(update, str(exc))


async def export_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_message is None:
        return
    args = getattr(context, "args", None) or []
    if len(args) != 2:
        await _reply(update, "Send BOOK_ID FORMAT\nFormats: txt, docx, epub, pdf\nExample: 1 docx")
        return
    try:
        book_id = parse_positive_integer(args[0], "Book ID")
        file_format = args[1].lower().strip()
        if file_format not in SUPPORTED_EXPORT_FORMATS:
            raise ValueError("Unsupported format. Choose txt, docx, epub, or pdf.")
        user_id = get_user_id(update)
        book = get_book_or_raise(book_id, user_id)
        await _reply(update, f"📦 Preparing the {file_format.upper()} manuscript for {book['title']}…")
        output_path = await asyncio.to_thread(exporter.export_book, book_id, user_id, file_format)
        if not output_path.exists():
            raise ValueError("The export file was not created.")
        with output_path.open("rb") as file_handle:
            await update.effective_message.reply_document(
                document=InputFile(file_handle, filename=output_path.name),
                caption=(
                    f"📚 {book['title']}\nFormat: {file_format.upper()}\n\n"
                    "Please proofread the exported file and verify formatting before uploading it to KDP."
                ),
                reply_markup=MAIN_KEYBOARD,
            )
    except (ValueError, exporter.ExportError) as exc:
        await _reply(update, f"Export failed:\n{exc}")
    except Exception:
        logger.exception("Manuscript export failed.")
        await _reply(update, "An unexpected error occurred during export. Your saved chapters have not been removed. Check Railway logs.")


async def kdpcheck_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    checklist = (
        "🧾 KDP PRE-PUBLICATION CHECKLIST\n\n"
        "CONTENT AND ORIGINALITY\n"
        "☐ Read the complete manuscript yourself.\n"
        "☐ Check continuity, names, dates, timelines, and repeated passages.\n"
        "☐ Verify facts, quotations, permissions, and rights to all included material.\n"
        "☐ Make sure the title, subtitle, blurb, categories, keywords, and cover accurately describe the book.\n\n"
        "AI DISCLOSURE\n"
        "☐ During KDP publishing, accurately answer the AI-generated-content question.\n"
        "Amazon KDP distinguishes AI-generated content (including text, images, or translations created by AI) "
        "from AI-assisted work where you created the content and used AI to brainstorm, edit, or improve it. "
        "KDP says AI-generated content must be disclosed; AI-assisted content does not require that disclosure. "
        "Check the current KDP policy at publication time.\n\n"
        "FILE QUALITY\n"
        "☐ Proofread the exported DOCX/EPUB/PDF on a separate device.\n"
        "☐ Check front matter, table of contents where applicable, chapter breaks, page layout, and cover dimensions.\n"
        "☐ Use KDP's Previewer and resolve warnings before publishing.\n"
        "☐ Keep your source manuscript and backups.\n\n"
        "MARKET REALITY\n"
        "☐ Check comparable books, reader expectations, cover quality, positioning, and your actual costs.\n"
        "☐ Do not assume an AI-generated idea guarantees sales.\n\n"
        "This is a practical checklist, not legal advice or a substitute for KDP's current rules."
    )
    await _reply(update, checklist)


# ---------------------------------------------------------------------------
# Guided menu router
# ---------------------------------------------------------------------------

async def menu_button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None or not message.text:
        return
    text = message.text.strip()
    normal = text.casefold().strip()

    # Natural shortcuts: users do not need to know slash commands.
    if normal in {"new book", "new story", "new idea", "generate ideas", "fresh book ideas", "write a new book"}:
        context.user_data.pop("pending_menu_action", None)
        await ideas_command(update, context)
        return
    if normal in {"my books", "books"}:
        context.user_data.pop("pending_menu_action", None)
        await books_command(update, context)
        return
    if normal in {"help", "menu"}:
        context.user_data.pop("pending_menu_action", None)
        await help_command(update, context)
        return

    # A newly pressed menu button overrides the previous pending prompt.
    if text in MENU_ACTIONS:
        action, prompt, _ = MENU_ACTIONS[text]
        context.user_data.pop("pending_menu_action", None)
        if action == "ideas":
            await ideas_command(update, context)
            return
        if action == "books":
            await books_command(update, context)
            return
        if action == "help":
            await help_command(update, context)
            return
        if action == "kdpcheck":
            await kdpcheck_command(update, context)
            return
        if action == "cancel":
            context.user_data.pop("pending_menu_action", None)
            await _reply(update, "✅ Cancelled the current menu step. Your saved books and chapters were not changed.")
            return
        context.user_data["pending_menu_action"] = action
        await _reply(update, prompt)
        return

    pending = context.user_data.get("pending_menu_action")
    if pending:
        context.user_data.pop("pending_menu_action", None)
        args = text.split()
        proxy = _ContextWithArgs(context, args)
        handler = COMMAND_BY_MENU_STATE.get(pending)
        if handler is None:
            await _reply(update, "That menu action is not available. Tap ❓ Help and try again.")
            return
        try:
            await handler(update, proxy)
        except Exception:
            logger.exception("Guided menu action %s failed.", pending)
            await _reply(update, "That step failed unexpectedly. Your existing project data was not intentionally changed. Tap the relevant menu button and try again.")
        return

    await _reply(
        update,
        "I didn't recognise that message as a menu action. Use the buttons below, or type ‘new book’ to generate fresh concepts. Tap ❓ Help for guidance.",
    )


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop("pending_menu_action", None)
    await _reply(update, "✅ Cancelled the current step. Your saved work is unchanged.")


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    error = context.error
    if error is None:
        logger.error("Telegram application reported an unknown error.")
        return
    logger.error(
        "Unhandled Telegram application error.",
        exc_info=(type(error), error, error.__traceback__),
    )


# ---------------------------------------------------------------------------
# Startup and handler registration
# ---------------------------------------------------------------------------

COMMAND_BY_MENU_STATE.update(
    {
        "manual_newbook": newbook_command,
        "outline": outline_command,
        "write": write_command,
        "chapter": chapter_command,
        "review": review_command,
        "revise": revise_command,
        "status": status_command,
        "export": export_command,
    }
)


def main() -> None:
    config.validate_required_settings()
    storage.initialize_storage()

    application = (
        Application.builder()
        .token(config.TELEGRAM_BOT_TOKEN)
        .build()
    )

    # Existing slash commands remain supported.
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("ideas", ideas_command))
    application.add_handler(CommandHandler("newbook", newbook_command))
    application.add_handler(CommandHandler("books", books_command))
    application.add_handler(CommandHandler("outline", outline_command))
    application.add_handler(CommandHandler("write", write_command))
    application.add_handler(CommandHandler("chapter", chapter_command))
    application.add_handler(CommandHandler("review", review_command))
    application.add_handler(CommandHandler("revise", revise_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("export", export_command))
    application.add_handler(CommandHandler("kdpcheck", kdpcheck_command))
    application.add_handler(CommandHandler("cancel", cancel_command))

    # Inline idea-selection buttons must be registered separately from text buttons.
    application.add_handler(CallbackQueryHandler(idea_callback, pattern=r"^idea:"))

    # Commands are filtered out here, so this handler only processes ordinary text/menu buttons.
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, menu_button_handler))
    application.add_error_handler(error_handler)

    logger.info("Telegram Book Studio is starting.")
    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=False,
    )


if __name__ == "__main__":
    main()
