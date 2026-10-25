"""
AI writing engine for Telegram Book Studio.

Responsibilities:
- Generate structured book outlines.
- Maintain story continuity.
- Write individual chapters.
- Review drafts for weaknesses.
- Revise chapters.
- Generate chapter summaries.
- Communicate with OpenRouter using asynchronous HTTP requests.
"""

import json
import re
from typing import Any

import httpx

import config


OPENROUTER_ENDPOINT = (
    "https://openrouter.ai/api/v1/chat/completions"
)


class WritingEngineError(Exception):
    """Raised when the writing engine cannot complete a task."""


def _extract_json(text: str) -> dict:
    """
    Extract a JSON object from an AI response.

    Supports ordinary JSON and JSON surrounded by Markdown fences.
    """
    text = text.strip()

    if text.startswith("```"):
        text = re.sub(
            r"^```(?:json)?\s*",
            "",
            text,
            flags=re.IGNORECASE,
        )
        text = re.sub(
            r"\s*```$",
            "",
            text,
        )

    try:
        result = json.loads(text)

        if isinstance(result, dict):
            return result

    except json.JSONDecodeError:
        pass

    start = text.find("{")
    end = text.rfind("}")

    if start != -1 and end > start:
        candidate = text[start:end + 1]

        try:
            result = json.loads(candidate)

            if isinstance(result, dict):
                return result

        except json.JSONDecodeError:
            pass

    raise WritingEngineError(
        "The AI returned invalid JSON. "
        "Please retry the operation."
    )


class WritingEngine:
    """Asynchronous AI writing engine."""

    def __init__(self) -> None:
        if not config.OPENROUTER_API_KEY:
            raise WritingEngineError(
                "OPENROUTER_API_KEY is not configured."
            )

        self.api_key = config.OPENROUTER_API_KEY
        self.model = config.OPENROUTER_MODEL

        self.timeout = httpx.Timeout(
            float(config.REQUEST_TIMEOUT_SECONDS),
            connect=20.0,
        )

    def _headers(self) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        if config.OPENROUTER_SITE_URL:
            headers["HTTP-Referer"] = (
                config.OPENROUTER_SITE_URL
            )

        if config.OPENROUTER_APP_NAME:
            headers["X-Title"] = (
                config.OPENROUTER_APP_NAME
            )

        return headers

    async def _request(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.7,
        max_tokens: int = 6000,
    ) -> str:
        """Send a chat-completion request to OpenRouter."""
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }

        try:
            async with httpx.AsyncClient(
                timeout=self.timeout,
                follow_redirects=True,
            ) as client:
                response = await client.post(
                    OPENROUTER_ENDPOINT,
                    headers=self._headers(),
                    json=payload,
                )

        except httpx.TimeoutException as exc:
            raise WritingEngineError(
                "The AI provider took too long to respond. "
                "Please retry."
            ) from exc

        except httpx.HTTPError as exc:
            raise WritingEngineError(
                "Could not connect to the AI provider. "
                "Check the network and try again."
            ) from exc

        if response.status_code == 401:
            raise WritingEngineError(
                "OpenRouter rejected the API key. "
                "Check OPENROUTER_API_KEY."
            )

        if response.status_code == 402:
            raise WritingEngineError(
                "OpenRouter reports insufficient credits "
                "or a payment requirement for this request."
            )

        if response.status_code == 429:
            raise WritingEngineError(
                "The AI provider is rate-limiting requests. "
                "Wait briefly and try again."
            )

        if response.status_code >= 500:
            raise WritingEngineError(
                "The AI provider is temporarily unavailable. "
                "Please retry later."
            )

        if response.status_code >= 400:
            detail = response.text[:500]

            raise WritingEngineError(
                f"AI request failed with HTTP "
                f"{response.status_code}: {detail}"
            )

        try:
            data = response.json()
            content = data["choices"][0]["message"]["content"]

        except (
            ValueError,
            KeyError,
            IndexError,
            TypeError,
        ) as exc:
            raise WritingEngineError(
                "The AI provider returned an unexpected response."
            ) from exc

        if isinstance(content, list):
            content = "".join(
                item.get("text", "")
                for item in content
                if isinstance(item, dict)
            )

        if not isinstance(content, str) or not content.strip():
            raise WritingEngineError(
                "The AI provider returned an empty response."
            )

        return content.strip()

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
        """
        Generate a complete book outline.

        Returns a dictionary containing:
        - title
        - genre
        - audience
        - synopsis
        - characters
        - worldbuilding
        - chapters
        """
        if not 1 <= chapter_count <= config.MAX_CHAPTERS:
            raise ValueError(
                f"Chapter count must be between 1 and "
                f"{config.MAX_CHAPTERS}."
            )

        if not title.strip():
            raise ValueError("A book title is required.")

        if not premise.strip():
            raise ValueError(
                "A premise is required to create an outline."
            )

        system_prompt = """
You are a professional developmental editor, literary
story architect, and experienced commercial fiction writer.

Your task is to design a coherent, compelling book outline.

Prioritize:
- A strong central premise.
- A clear narrative promise.
- Specific character motivations and flaws.
- Meaningful conflict and consequences.
- Escalating stakes.
- Cause-and-effect plotting.
- Distinct character voices.
- Appropriate pacing for the genre.
- Originality rather than familiar clichés.
- A satisfying ending consistent with the story.

Do not pad the outline with repetitive events.

Return valid JSON only. Do not use Markdown fences.

Required JSON structure:
{
  "title": "Book title",
  "genre": "Genre",
  "audience": "Target audience",
  "synopsis": "A detailed synopsis",
  "characters": [
    {
      "name": "Character name",
      "role": "Narrative role",
      "motivation": "What they want",
      "flaw": "Their central weakness",
      "arc": "How they change"
    }
  ],
  "worldbuilding": [
    "Important setting fact"
  ],
  "themes": [
    "Theme"
  ],
  "chapters": [
    {
      "number": 1,
      "title": "Chapter title",
      "purpose": "Why this chapter exists",
      "summary": "Events in this chapter",
      "emotional_turn": "How the emotional situation changes",
      "continuity_notes": "Facts later chapters must preserve"
    }
  ]
}

Return exactly the requested number of chapters.
Every chapter must have a distinct narrative purpose.
"""

        user_prompt = f"""
BOOK TITLE:
{title}

GENRE:
{genre}

TARGET AUDIENCE:
{audience}

PREMISE:
{premise}

NUMBER OF CHAPTERS:
{chapter_count}

ADDITIONAL INSTRUCTIONS:
{additional_instructions or "None"}

Create the complete outline now.
"""

        raw = await self._request(
            [
                {
                    "role": "system",
                    "content": system_prompt.strip(),
                },
                {
                    "role": "user",
                    "content": user_prompt.strip(),
                },
            ],
            temperature=0.7,
            max_tokens=7000,
        )

        outline = _extract_json(raw)

        chapters = outline.get("chapters")

        if not isinstance(chapters, list):
            raise WritingEngineError(
                "The generated outline has no valid chapter list."
            )

        if len(chapters) != chapter_count:
            raise WritingEngineError(
                f"The outline contains {len(chapters)} chapters, "
                f"but {chapter_count} were requested. "
                "Retry outline generation."
            )

        for index, chapter in enumerate(chapters, start=1):
            if not isinstance(chapter, dict):
                raise WritingEngineError(
                    "An outline chapter is not a valid object."
                )

            chapter["number"] = index

            if not chapter.get("title"):
                chapter["title"] = f"Chapter {index}"

            if not chapter.get("summary"):
                raise WritingEngineError(
                    f"Chapter {index} has no summary."
                )

        outline["title"] = title.strip()
        outline["genre"] = genre
        outline["audience"] = audience

        return outline

    async def write_chapter(
        self,
        *,
        book: dict,
        outline: dict,
        chapter_number: int,
        previous_chapters: list[dict] | None = None,
        story_bible: dict | None = None,
        target_words: int = 1800,
        additional_instructions: str = "",
    ) -> dict[str, Any]:
        """
        Write one chapter using the outline and existing story context.

        Previous chapters are supplied to maintain continuity.
        """
        chapters = outline.get("chapters", [])

        chapter_plan = next(
            (
                chapter
                for chapter in chapters
                if chapter.get("number") == chapter_number
            ),
            None,
        )

        if chapter_plan is None:
            raise ValueError(
                f"Chapter {chapter_number} is not in the outline."
            )

        if not 400 <= target_words <= config.MAX_CHAPTER_WORDS:
            raise ValueError(
                f"Target words must be between 400 and "
                f"{config.MAX_CHAPTER_WORDS}."
            )

        previous_chapters = previous_chapters or []
        story_bible = story_bible or {}

        previous_context = []

        for chapter in previous_chapters[-4:]:
            previous_context.append(
                {
                    "chapter_number": chapter.get(
                        "chapter_number"
                    ),
                    "title": chapter.get("title"),
                    "summary": chapter.get("summary", ""),
                    "ending_excerpt": chapter.get(
                        "content", ""
                    )[-1800:],
                }
            )

        system_prompt = """
You are an accomplished professional novelist.

Write the requested chapter as polished prose, not as an
outline, summary, screenplay, or explanation of your process.

CRAFT REQUIREMENTS:

1. Begin with an engaging situation, tension, question,
   or meaningful continuation appropriate to the story.
2. Use concrete details rather than generic descriptions.
3. Give characters distinct voices and believable motives.
4. Make dialogue sound natural and purposeful.
5. Vary sentence structure and paragraph rhythm.
6. Ensure scenes change the situation or deepen character.
7. Maintain continuity with the outline and prior chapters.
8. Avoid repeating information the reader already knows.
9. Avoid filler, clichés, artificial melodrama, and
   repetitive internal monologues.
10. Respect the genre and target audience.
11. End at a natural, compelling transition consistent
    with the planned chapter.
12. Never claim a word count you have not achieved.

Do not summarize events that should be dramatized.
Do not rush through important emotional moments.
Do not resolve major conflicts prematurely.
Do not introduce major new characters without a reason.

Return valid JSON only.

Required structure:
{
  "title": "Chapter title",
  "content": "The complete chapter prose",
  "summary": "A concise factual summary",
  "continuity_facts": [
    "Important fact established in this chapter"
  ]
}
"""

        user_prompt = f"""
BOOK:
{book.get("title", outline.get("title", "Untitled"))}

GENRE:
{book.get("genre", outline.get("genre", "Fiction"))}

TARGET AUDIENCE:
{book.get("audience", outline.get("audience", "Adult readers"))}

BOOK SYNOPSIS:
{outline.get("synopsis", "")}

CHARACTER PROFILES:
{json.dumps(outline.get("characters", []), ensure_ascii=False)}

WORLD-BUILDING:
{json.dumps(outline.get("worldbuilding", []), ensure_ascii=False)}

PERSISTENT STORY FACTS:
{json.dumps(story_bible, ensure_ascii=False)}

CHAPTER PLAN:
{json.dumps(chapter_plan, ensure_ascii=False)}

PREVIOUS CHAPTER CONTEXT:
{json.dumps(previous_context, ensure_ascii=False)}

TARGET LENGTH:
Approximately {target_words} words.

ADDITIONAL INSTRUCTIONS:
{additional_instructions or "None"}

Write the complete chapter.
"""

        raw = await self._request(
            [
                {
                    "role": "system",
                    "content": system_prompt.strip(),
                },
                {
                    "role": "user",
                    "content": user_prompt.strip(),
                },
            ],
            temperature=0.8,
            max_tokens=min(
                8000,
                max(2500, target_words * 2),
            ),
        )

        result = _extract_json(raw)

        title = result.get("title")
        content = result.get("content")
        summary = result.get("summary")

        if not isinstance(title, str) or not title.strip():
            raise WritingEngineError(
                "The generated chapter has no valid title."
            )

        if not isinstance(content, str) or len(content.split()) < 100:
            raise WritingEngineError(
                "The generated chapter is too short or empty."
            )

        if not isinstance(summary, str) or not summary.strip():
            raise WritingEngineError(
                "The generated chapter has no summary."
            )

        result["title"] = title.strip()
        result["content"] = content.strip()
        result["summary"] = summary.strip()
        result["word_count"] = len(content.split())
        result["chapter_number"] = chapter_number

        if not isinstance(result.get("continuity_facts"), list):
            result["continuity_facts"] = []

        return result

    async def review_chapter(
        self,
        *,
        book: dict,
        outline: dict,
        chapter_number: int,
        chapter_title: str,
        content: str,
        previous_chapters: list[dict] | None = None,
    ) -> dict[str, Any]:
        """
        Critically evaluate a chapter without automatically
        replacing the original manuscript.

        Returns a structured editorial report.
        """
        if not content.strip():
            raise ValueError("Chapter content cannot be empty.")

        chapters = outline.get("chapters", [])

        chapter_plan = next(
            (
                chapter
                for chapter in chapters
                if chapter.get("number") == chapter_number
            ),
            {},
        )

        context = [
            {
                "chapter_number": item.get("chapter_number"),
                "title": item.get("title"),
                "summary": item.get("summary", ""),
            }
            for item in (previous_chapters or [])[-5:]
        ]

        system_prompt = """
You are a rigorous developmental editor and line editor.

Evaluate the submitted chapter honestly. Do not praise weak
writing merely to be encouraging.

Assess:
- Plot progression.
- Character motivation and consistency.
- Emotional credibility.
- Scene construction.
- Dialogue.
- Sentence-level prose.
- Pacing and repetition.
- Continuity with the outline.
- Genre expectations.
- Whether the ending advances the story.

Distinguish genuine problems from stylistic preferences.
Do not invent continuity errors unsupported by the material.
Do not rewrite the chapter.

Return valid JSON only:
{
  "overall_assessment": "Concise assessment",
  "strengths": ["Specific strength"],
  "issues": [
    {
      "severity": "high, medium, or low",
      "problem": "Specific problem",
      "evidence": "Relevant example or location",
      "recommendation": "Concrete correction"
    }
  ],
  "continuity_warnings": ["Specific warning"],
  "revision_priorities": ["Highest-priority correction"],
  "ready_for_revision": true
}
"""

        user_prompt = f"""
BOOK:
{book.get("title", "")}

GENRE:
{book.get("genre", "")}

CHAPTER NUMBER:
{chapter_number}

CHAPTER TITLE:
{chapter_title}

CHAPTER PLAN:
{json.dumps(chapter_plan, ensure_ascii=False)}

PREVIOUS CHAPTER SUMMARIES:
{json.dumps(context, ensure_ascii=False)}

CHAPTER TO REVIEW:
{content}

Provide a rigorous editorial assessment.
"""

        raw = await self._request(
            [
                {
                    "role": "system",
                    "content": system_prompt.strip(),
                },
                {
                    "role": "user",
                    "content": user_prompt.strip(),
                },
            ],
            temperature=0.2,
            max_tokens=3500,
        )

        report = _extract_json(raw)

        if not isinstance(report.get("issues"), list):
            raise WritingEngineError(
                "The editorial report is missing its issues list."
            )

        return report

    async def revise_chapter(
        self,
        *,
        book: dict,
        outline: dict,
        chapter_number: int,
        chapter_title: str,
        content: str,
        review: dict | None = None,
        revision_instructions: str = "",
    ) -> dict[str, Any]:
        """
        Revise a chapter using editorial feedback.

        The caller must explicitly save the revised content.
        The previous version is not discarded by this method.
        """
        if not content.strip():
            raise ValueError("Chapter content cannot be empty.")

        if not review and not revision_instructions.strip():
            raise ValueError(
                "Provide an editorial report or revision instructions."
            )

        system_prompt = """
You are a meticulous professional novelist revising your own work
after receiving editorial feedback.

Preserve the story's identity, point of view, tense, character
voices, established facts, and intended emotional effect.

Fix substantive weaknesses rather than merely replacing words.

Prioritize:
- Clearer motivations.
- Stronger scene progression.
- Natural dialogue.
- Specific, controlled prose.
- Appropriate pacing.
- Removal of redundant passages.
- Emotional credibility.
- Continuity.
- A stronger chapter ending where necessary.

Do not introduce unsupported plot changes.
Do not remove essential scenes merely to shorten the chapter.
Do not turn distinctive prose into generic writing.
Do not claim that every editorial suggestion must be followed.

Return valid JSON only:
{
  "title": "Chapter title",
  "content": "The complete revised chapter",
  "summary": "A factual summary",
  "changes_made": ["Important revision"],
  "continuity_facts": ["Established fact"]
}
"""

        user_prompt = f"""
BOOK:
{book.get("title", "")}

GENRE:
{book.get("genre", "")}

CHAPTER NUMBER:
{chapter_number}

CHAPTER TITLE:
{chapter_title}

OUTLINE:
{json.dumps(outline, ensure_ascii=False)}

EDITORIAL REPORT:
{json.dumps(review or {}, ensure_ascii=False)}

ADDITIONAL REVISION INSTRUCTIONS:
{revision_instructions or "Address the editorial report."}

ORIGINAL CHAPTER:
{content}

Revise the complete chapter.
"""

        raw = await self._request(
            [
                {
                    "role": "system",
                    "content": system_prompt.strip(),
                },
                {
                    "role": "user",
                    "content": user_prompt.strip(),
                },
            ],
            temperature=0.6,
            max_tokens=8000,
        )

        result = _extract_json(raw)

        revised_content = result.get("content")

        if (
            not isinstance(revised_content, str)
            or len(revised_content.split()) < 100
        ):
            raise WritingEngineError(
                "The revised chapter is missing or too short."
            )

        result["title"] = (
            result.get("title") or chapter_title
        ).strip()

        result["content"] = revised_content.strip()

        if not isinstance(result.get("summary"), str):
            result["summary"] = ""

        result["word_count"] = len(
            result["content"].split()
        )

        result["chapter_number"] = chapter_number

        return result

    async def summarize_chapter(
        self,
        *,
        chapter_title: str,
        content: str,
    ) -> dict[str, Any]:
        """
        Extract a compact summary and continuity facts.

        Useful for keeping later chapters consistent without
        repeatedly sending the entire manuscript to the model.
        """
        if not content.strip():
            raise ValueError("Chapter content cannot be empty.")

        system_prompt = """
You are a story continuity editor.

Summarize the actual events of the chapter. Record facts that
future chapters must preserve, including character decisions,
relationships, promises, injuries, objects, locations, dates,
unresolved conflicts, and information known by each character.

Do not invent facts or future events.

Return valid JSON only:
{
  "summary": "Concise factual summary",
  "continuity_facts": ["Established fact"],
  "unresolved_threads": ["Unresolved issue"],
  "character_changes": [
    {
      "character": "Name",
      "change": "Specific change"
    }
  ]
}
"""

        raw = await self._request(
            [
                {
                    "role": "system",
                    "content": system_prompt.strip(),
                },
                {
                    "role": "user",
                    "content": (
                        f"CHAPTER TITLE:\n{chapter_title}\n\n"
                        f"CHAPTER TEXT:\n{content}"
                    ),
                },
            ],
            temperature=0.2,
            max_tokens=2500,
        )

        result = _extract_json(raw)

        if not isinstance(result.get("summary"), str):
            raise WritingEngineError(
                "The chapter summary is missing."
            )

        for key in (
            "continuity_facts",
            "unresolved_threads",
            "character_changes",
        ):
            if not isinstance(result.get(key), list):
                result[key] = []

        return result
