"""AI writing engine for Author Telegram Book Studio.

Compatible with the current main.py: exposes WritingEngineError, _request,
generate_outline, write_chapter, review_chapter, revise_chapter and
summarize_chapter. Uses the OpenRouter chat-completions API configured in
config.py and does not silently switch providers or require another API key.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

import httpx

import config

logger = logging.getLogger(__name__)

OPENROUTER_ENDPOINT = f"{config.OPENROUTER_BASE_URL}/chat/completions"


class WritingEngineError(Exception):
    """A safe, user-readable error from the writing engine or AI provider."""


def _extract_json(text: str) -> dict[str, Any]:
    """Extract one JSON object from a provider response, including fenced JSON."""
    if not isinstance(text, str) or not text.strip():
        raise WritingEngineError("The AI returned an empty response. Please retry.")
    cleaned = text.strip().lstrip("\ufeff")
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        result = json.loads(cleaned)
        if isinstance(result, dict):
            return result
    except json.JSONDecodeError:
        pass
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start >= 0 and end > start:
        try:
            result = json.loads(cleaned[start : end + 1])
            if isinstance(result, dict):
                return result
        except json.JSONDecodeError:
            pass
    raise WritingEngineError("The AI returned malformed JSON. Please retry the operation.")


def _as_text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _json_or_default(value: Any, default: Any) -> Any:
    return value if isinstance(value, type(default)) else default


class WritingEngine:
    """Async AI engine for concept development, outlining, drafting and editing."""

    def __init__(self) -> None:
        if not config.OPENROUTER_API_KEY:
            raise WritingEngineError("OPENROUTER_API_KEY is not configured in Railway Variables.")
        if not config.OPENROUTER_MODEL:
            raise WritingEngineError("OPENROUTER_MODEL is empty. Set it in Railway Variables.")
        self.api_key = config.OPENROUTER_API_KEY
        self.model = config.OPENROUTER_MODEL
        self.timeout = httpx.Timeout(
            float(config.REQUEST_TIMEOUT_SECONDS),
            connect=float(config.OPENROUTER_CONNECT_TIMEOUT_SECONDS),
        )

    def _headers(self) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-Title": config.OPENROUTER_APP_NAME,
        }
        if config.OPENROUTER_SITE_URL:
            headers["HTTP-Referer"] = config.OPENROUTER_SITE_URL
        return headers

    async def _request(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.7,
        max_tokens: int = 6000,
    ) -> str:
        """Send a bounded chat-completions request and convert errors to clear messages."""
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": max(0.0, min(float(temperature), 1.5)),
            "max_tokens": max(64, min(int(max_tokens), 12000)),
        }
        retries = config.OPENROUTER_MAX_RETRIES
        for attempt in range(retries + 1):
            try:
                async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True) as client:
                    response = await client.post(
                        OPENROUTER_ENDPOINT,
                        headers=self._headers(),
                        json=payload,
                    )
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                if attempt < retries:
                    await asyncio.sleep(1.0 * (attempt + 1))
                    continue
                if isinstance(exc, httpx.TimeoutException):
                    raise WritingEngineError(
                        "The AI provider timed out. Try again; if it repeats, choose a faster available model in OPENROUTER_MODEL."
                    ) from exc
                raise WritingEngineError(
                    "Could not connect to OpenRouter. Check Railway logs and network availability."
                ) from exc
            except httpx.HTTPError as exc:
                raise WritingEngineError(f"The OpenRouter request failed: {type(exc).__name__}.") from exc

            if response.status_code >= 500 and attempt < retries:
                await asyncio.sleep(1.0 * (attempt + 1))
                continue
            if response.status_code == 401:
                raise WritingEngineError("OpenRouter rejected the API key. Check OPENROUTER_API_KEY in Railway Variables.")
            if response.status_code == 402:
                raise WritingEngineError("OpenRouter reports insufficient credits or a payment requirement for this model.")
            if response.status_code == 403:
                raise WritingEngineError("OpenRouter refused this request. Check model access, provider restrictions, and account settings.")
            if response.status_code == 404:
                raise WritingEngineError(f"OpenRouter could not find model '{self.model}'. Check OPENROUTER_MODEL.")
            if response.status_code == 408:
                raise WritingEngineError("OpenRouter timed out. Please retry.")
            if response.status_code == 429:
                raise WritingEngineError("OpenRouter is rate-limiting this request. Wait briefly and try again.")
            if response.status_code >= 400:
                # Do not return credentials or request headers to the Telegram user.
                try:
                    detail = response.json().get("error", {}).get("message", "")
                except (ValueError, AttributeError):
                    detail = ""
                detail = re.sub(r"\s+", " ", str(detail))[:280]
                suffix = f" Details: {detail}" if detail else ""
                raise WritingEngineError(f"OpenRouter request failed with HTTP {response.status_code}.{suffix}")

            try:
                data = response.json()
                choices = data.get("choices") or []
                content = choices[0]["message"]["content"]
            except (ValueError, KeyError, IndexError, TypeError, AttributeError) as exc:
                raise WritingEngineError("OpenRouter returned an unexpected response. Check the configured model and retry.") from exc

            if isinstance(content, list):
                content = "".join(
                    item.get("text", "") for item in content if isinstance(item, dict)
                )
            if not isinstance(content, str) or not content.strip():
                raise WritingEngineError("The AI model returned no text. Try again or choose another OPENROUTER_MODEL.")
            return content.strip()
        raise WritingEngineError("The AI provider could not complete the request. Please retry.")

    async def generate_outline(
        self,
        *,
        title: str,
        genre: str,
        audience: str,
        premise: str,
        chapter_count: int = 12,
        additional_instructions: str = "",
    ) -> dict[str, Any]:
        if not 1 <= chapter_count <= config.MAX_CHAPTERS:
            raise ValueError(f"Chapter count must be between 1 and {config.MAX_CHAPTERS}.")
        title, premise = _as_text(title), _as_text(premise)
        if not title:
            raise ValueError("A book title is required.")
        if not premise:
            raise ValueError("A premise is required to create an outline.")

        system = """You are a demanding commercial-fiction story architect and developmental editor.
Create an original, causally coherent novel outline suitable for a serious human author to develop.
Prioritize a specific protagonist goal, credible opposition, consequential decisions, escalation,
emotional change, distinct character voices, varied scene functions, strong pacing and a satisfying
ending. Avoid filler, repeated chapter beats, convenient coincidences, generic exposition and
clichés without a fresh mechanism. Do not imitate any living author's style or any existing franchise.
Return valid JSON only, no Markdown fences, using this schema:
{"title":"...","genre":"...","audience":"...","synopsis":"...","characters":[{"name":"...","role":"...","motivation":"...","flaw":"...","arc":"..."}],"worldbuilding":["..."],"themes":["..."],"chapters":[{"number":1,"title":"...","purpose":"...","summary":"...","emotional_turn":"...","continuity_notes":"..."}]}
The chapters array must contain exactly the requested number of chapters, each with a distinct narrative purpose."""
        prompt = f"""BOOK TITLE: {title}
GENRE: {genre}
TARGET AUDIENCE: {audience}
PREMISE: {premise}
NUMBER OF CHAPTERS: {chapter_count}
ADDITIONAL INSTRUCTIONS: {additional_instructions or 'None'}
Design the whole story, including a clear ending. Return exactly {chapter_count} chapter plans."""
        last_error = "The outline was invalid."
        for attempt in range(2):
            raw = await self._request(
                [{"role": "system", "content": system}, {"role": "user", "content": prompt + ("\nRETRY: Correct the JSON and ensure exactly the requested chapter count." if attempt else "")}],
                temperature=0.65 if attempt == 0 else 0.45,
                max_tokens=min(10000, max(3500, chapter_count * 400)),
            )
            try:
                outline = _extract_json(raw)
                chapters = outline.get("chapters")
                if not isinstance(chapters, list) or len(chapters) != chapter_count:
                    raise WritingEngineError(f"Expected {chapter_count} chapters; received {len(chapters) if isinstance(chapters, list) else 'no valid chapter list'}.")
                for index, chapter in enumerate(chapters, start=1):
                    if not isinstance(chapter, dict) or not _as_text(chapter.get("summary")):
                        raise WritingEngineError(f"Chapter {index} is missing its summary.")
                    chapter["number"] = index
                    chapter["title"] = _as_text(chapter.get("title")) or f"Chapter {index}"
                    chapter["purpose"] = _as_text(chapter.get("purpose"))
                    chapter["emotional_turn"] = _as_text(chapter.get("emotional_turn"))
                    chapter["continuity_notes"] = _as_text(chapter.get("continuity_notes"))
                outline["title"] = title
                outline["genre"] = _as_text(genre) or "General Fiction"
                outline["audience"] = _as_text(audience) or "Adult readers"
                outline["synopsis"] = _as_text(outline.get("synopsis"))
                outline["characters"] = _json_or_default(outline.get("characters"), [])
                outline["worldbuilding"] = _json_or_default(outline.get("worldbuilding"), [])
                outline["themes"] = _json_or_default(outline.get("themes"), [])
                return outline
            except WritingEngineError as exc:
                last_error = str(exc)
        raise WritingEngineError(f"Could not create a valid outline after two attempts. {last_error}")

    async def write_chapter(
        self,
        *,
        book: dict[str, Any],
        outline: dict[str, Any],
        chapter_number: int,
        previous_chapters: list[dict[str, Any]] | None = None,
        story_bible: dict[str, Any] | None = None,
        target_words: int = 1800,
        additional_instructions: str = "",
    ) -> dict[str, Any]:
        chapters = outline.get("chapters", [])
        plan = next((item for item in chapters if isinstance(item, dict) and item.get("number") == chapter_number), None)
        if plan is None:
            raise ValueError(f"Chapter {chapter_number} is not in the saved outline.")
        if not 400 <= target_words <= config.MAX_CHAPTER_WORDS:
            raise ValueError(f"Target words must be between 400 and {config.MAX_CHAPTER_WORDS}.")
        earlier = []
        for item in (previous_chapters or [])[-4:]:
            earlier.append({
                "chapter_number": item.get("chapter_number"),
                "title": item.get("title"),
                "summary": item.get("summary", ""),
                "ending_excerpt": str(item.get("content", ""))[-1500:],
            })
        system = """You are an accomplished professional novelist writing a complete chapter of an original book.
Write polished dramatic prose, never an outline, summary, screenplay or explanation. Start with an
active, compelling moment appropriate to the story. Use concrete details, purposeful scenes, natural
dialogue, distinct character voices, varied sentence rhythm and emotionally credible choices. Respect
point of view, tense, prior events and the outline. Each scene must change the situation, reveal character,
or create pressure. Avoid clichés, filler, repeated explanations, repetitive internal monologue and
artificial cliffhangers. Do not resolve the book's central conflict prematurely. Aim near the target
length, but do not pad. Return JSON only: {"title":"...","content":"complete chapter prose","summary":"factual summary","continuity_facts":["..." ]}. Escape JSON correctly."""
        prompt = f"""BOOK: {book.get('title', outline.get('title', 'Untitled'))}
GENRE: {book.get('genre', outline.get('genre', 'Fiction'))}
AUDIENCE: {book.get('audience', outline.get('audience', 'Adult readers'))}
SYNOPSIS: {outline.get('synopsis', '')}
CHARACTERS: {json.dumps(outline.get('characters', []), ensure_ascii=False)}
WORLD-BUILDING: {json.dumps(outline.get('worldbuilding', []), ensure_ascii=False)}
STORY BIBLE / CONTINUITY: {json.dumps(story_bible or {}, ensure_ascii=False)}
CHAPTER PLAN: {json.dumps(plan, ensure_ascii=False)}
PREVIOUS CHAPTERS: {json.dumps(earlier, ensure_ascii=False)}
TARGET LENGTH: Approximately {target_words} words. Produce an actual chapter, not a synopsis.
ADDITIONAL INSTRUCTIONS: {additional_instructions or 'None'}"""
        raw = await self._request(
            [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
            temperature=0.8,
            max_tokens=min(10000, max(2500, target_words * 2)),
        )
        result = _extract_json(raw)
        chapter_title = _as_text(result.get("title")) or _as_text(plan.get("title")) or f"Chapter {chapter_number}"
        content = _as_text(result.get("content"))
        if len(content.split()) < 100:
            raise WritingEngineError("The generated chapter was too short to be a useful draft. Try again or lower the target length.")
        summary = _as_text(result.get("summary"))
        if not summary:
            # Don't silently fabricate a summary: the chapter can be retried instead.
            raise WritingEngineError("The generated chapter has no summary. Please retry.")
        result.update({
            "title": chapter_title,
            "content": content,
            "summary": summary,
            "word_count": len(content.split()),
            "chapter_number": chapter_number,
            "continuity_facts": _json_or_default(result.get("continuity_facts"), []),
        })
        return result

    async def review_chapter(
        self,
        *,
        book: dict[str, Any],
        outline: dict[str, Any],
        chapter_number: int,
        chapter_title: str,
        content: str,
        previous_chapters: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        if not _as_text(content):
            raise ValueError("Chapter content cannot be empty.")
        plan = next((item for item in outline.get("chapters", []) if isinstance(item, dict) and item.get("number") == chapter_number), {})
        prior = [{"chapter_number": item.get("chapter_number"), "title": item.get("title"), "summary": item.get("summary", "")} for item in (previous_chapters or [])[-5:]]
        system = """You are a rigorous developmental and line editor. Evaluate the chapter honestly and specifically.
Do not flatter weak prose or invent unsupported errors. Assess causal plot progression, character logic,
emotional credibility, scene turns, dialogue, sentence-level prose, pacing, repetition, point of view,
continuity, genre fit and whether the ending advances the book. Do not rewrite the chapter. Return valid
JSON only: {"overall_assessment":"...","strengths":["specific strength"],"issues":[{"severity":"high|medium|low","problem":"...","evidence":"...","recommendation":"..."}],"continuity_warnings":["..."],"revision_priorities":["..."],"ready_for_revision":true}."""
        prompt = f"BOOK: {book.get('title','')}\nGENRE: {book.get('genre','')}\nCHAPTER NUMBER: {chapter_number}\nTITLE: {chapter_title}\nPLAN: {json.dumps(plan, ensure_ascii=False)}\nPREVIOUS CHAPTER SUMMARIES: {json.dumps(prior, ensure_ascii=False)}\nCHAPTER TEXT:\n{content}\n\nReturn a specific editorial report based only on evidence in the text."
        report = _extract_json(await self._request(
            [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
            temperature=0.2, max_tokens=4500,
        ))
        report["overall_assessment"] = _as_text(report.get("overall_assessment")) or "Assessment returned without a summary."
        report["strengths"] = _json_or_default(report.get("strengths"), [])
        report["issues"] = _json_or_default(report.get("issues"), [])
        report["continuity_warnings"] = _json_or_default(report.get("continuity_warnings"), [])
        report["revision_priorities"] = _json_or_default(report.get("revision_priorities"), [])
        return report

    async def revise_chapter(
        self,
        *,
        book: dict[str, Any],
        outline: dict[str, Any],
        chapter_number: int,
        chapter_title: str,
        content: str,
        review: dict[str, Any] | None = None,
        revision_instructions: str = "",
    ) -> dict[str, Any]:
        if not _as_text(content):
            raise ValueError("Chapter content cannot be empty.")
        if not review and not _as_text(revision_instructions):
            raise ValueError("Provide an editorial report or revision instructions.")
        system = """You are a meticulous professional novelist revising a draft. Preserve plot identity, point of view,
tense, established facts, character voices and intended emotional effect. Fix substantive weaknesses,
not just synonyms. Improve scene progression, motivation, natural dialogue, controlled prose, pacing,
redundancy and continuity. Do not introduce unsupported plot twists, remove essential scenes merely to
shorten the chapter, or make distinctive prose generic. Return valid JSON only:
{"title":"...","content":"complete revised chapter","summary":"factual summary","changes_made":["..."],"continuity_facts":["..."]}."""
        prompt = f"""BOOK: {book.get('title','')}\nGENRE: {book.get('genre','')}\nCHAPTER: {chapter_number} — {chapter_title}
OUTLINE: {json.dumps(outline, ensure_ascii=False)}
EDITORIAL REPORT: {json.dumps(review or {}, ensure_ascii=False)}
ADDITIONAL REVISION INSTRUCTIONS: {revision_instructions or 'Address the supplied editorial report.'}
ORIGINAL CHAPTER:\n{content}\n\nReturn the full revised chapter, not a list of changes."""
        result = _extract_json(await self._request(
            [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
            temperature=0.55, max_tokens=10000,
        ))
        revised = _as_text(result.get("content"))
        if len(revised.split()) < 100:
            raise WritingEngineError("The revised chapter was missing or too short. The existing saved chapter has not been changed.")
        result["title"] = _as_text(result.get("title")) or chapter_title
        result["content"] = revised
        result["summary"] = _as_text(result.get("summary"))
        result["word_count"] = len(revised.split())
        result["chapter_number"] = chapter_number
        result["continuity_facts"] = _json_or_default(result.get("continuity_facts"), [])
        result["changes_made"] = _json_or_default(result.get("changes_made"), [])
        return result

    async def summarize_chapter(self, *, chapter_title: str, content: str) -> dict[str, Any]:
        if not _as_text(content):
            raise ValueError("Chapter content cannot be empty.")
        system = """You are a story-continuity editor. Summarize only actual chapter events and preserve facts future
chapters must respect: decisions, relationships, promises, injuries, objects, locations, chronology,
unresolved conflicts and who knows what. Do not invent events. Return valid JSON only:
{"summary":"concise factual summary","continuity_facts":["..."],"unresolved_threads":["..."],"character_changes":[{"character":"...","change":"..."}]}."""
        prompt = f"CHAPTER TITLE: {chapter_title}\nCHAPTER TEXT:\n{content}\nReturn summary and continuity data."
        result = _extract_json(await self._request(
            [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
            temperature=0.2, max_tokens=3000,
        ))
        if not _as_text(result.get("summary")):
            raise WritingEngineError("The summary response did not contain a usable summary.")
        for key in ("continuity_facts", "unresolved_threads", "character_changes"):
            result[key] = _json_or_default(result.get(key), [])
        return result
