"""Photo parsing with explicit failure modes.

The contract: return a ParseResult whose status is one of
ok / ambiguous / unreadable / rotated. The pipeline never proceeds on a status
that is not `ok`, because a confident diagnosis of a misread question is worse
than no diagnosis at all.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from llm import LLMResponseError, VisionUnavailableError

STATUSES = ("ok", "ambiguous", "unreadable", "rotated")
RETAKE_MESSAGE = "The photo could not be read reliably. Please retake it straight-on, well lit and uncropped."


@dataclass
class ParseResult:
    status: str = "unreadable"
    question: str = ""
    steps: List[str] = field(default_factory=list)
    notes: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    def to_dict(self) -> Dict[str, Any]:
        return {"status": self.status, "question": self.question, "steps": self.steps, "notes": self.notes}


def parse_photo(provider, image_path: str) -> ParseResult:
    """Ask the vision provider; any failure becomes an explicit, actionable status."""
    try:
        raw = provider.parse_photo(image_path)
    except VisionUnavailableError as exc:
        return ParseResult(status="unreadable", notes=str(exc))
    status = str(raw.get("status", "unreadable")).lower()
    if status not in STATUSES:
        status = "unreadable"
    question = raw.get("question") or ""
    steps = raw.get("steps") or []
    if isinstance(steps, str):
        steps = [steps]
    if not isinstance(question, str) or not isinstance(steps, list) or not all(isinstance(s, str) for s in steps):
        raise LLMResponseError("Photo transcript must contain question text and a list of written steps.")
    steps = [s for s in steps if s.strip()]
    if status == "ok" and (not question.strip() or not steps):
        status = "ambiguous" if question.strip() or steps else "unreadable"
    return ParseResult(
        status=status,
        question=question,
        steps=steps,
        notes=str(raw.get("notes", "") or ""),
    )


def from_text(question: str, steps: Optional[List[str]] = None) -> ParseResult:
    """Used by samples, CLI --question/--steps, and tests — no vision needed."""
    steps = steps or []
    if not question and not steps:
        return ParseResult(status="unreadable", notes=RETAKE_MESSAGE)
    return ParseResult(status="ok", question=question, steps=steps)


def from_sample(sample: Dict[str, Any]) -> ParseResult:
    return ParseResult(
        status=sample.get("parse_status", "ok" if sample.get("question") else "unreadable"),
        question=sample.get("question", ""),
        steps=sample.get("steps", []),
    )
