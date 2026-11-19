"""Pluggable inference layer.

Two providers implement the same interface:
  * OpenAICompatProvider  — any OpenAI-compatible endpoint (chat + vision + embeddings)
  * StubProvider          — offline rule baseline, used when no API key is configured

The stub is NOT a model. It is a deterministic rule engine that lets the whole
pipeline run, be tested and be demoed without network, and every output it produces
is labelled `provider: offline-rule-baseline` so it can never be mistaken for a
model diagnosis.
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import mimetypes
import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import requests
from dotenv import dotenv_values

ERROR_TYPES = ("conceptual", "procedural", "careless", "unreadable")

DIAGNOSE_SYSTEM = (
    "You are a mathematics and science diagnosis engine for school-level work. "
    "Given a question and the student's own working steps, locate the FIRST step where the "
    "reasoning broke, classify the error, and explain it in one or two sentences in the question's language. "
    "Reply with JSON only, matching this schema:\n"
    '{"broken_step": str, "error_type": "conceptual"|"procedural"|"careless"|"unreadable", '
    '"knowledge_point_hint": str, "confidence": float, "explanation": str}\n'
    "error_type is a closed set: conceptual = wrong idea, procedural = right idea executed wrongly, "
    "careless = arithmetic or sign slip, unreadable = the work cannot be read. "
    "Quote broken_step exactly as written by the student. If the work cannot be read, say so."
)

VISION_SYSTEM = (
    "You transcribe a photo of a school exercise. Reply with JSON only: "
    '{"status": "ok"|"ambiguous"|"unreadable"|"rotated", "question": str, "steps": [str], "notes": str}. '
    "Keep the student's own notation and mistakes exactly as written. "
    "If the image is blurry, cut off or rotated, set status accordingly — never guess missing text."
)


class LLMError(Exception):
    """Base class. Every failure mode below is actionable, never silent."""


class LLMNotConfiguredError(LLMError):
    pass


class LLMAuthError(LLMError):
    pass


class LLMQuotaError(LLMError):
    pass


class LLMNetworkError(LLMError):
    pass


class LLMResponseError(LLMError):
    pass


class VisionUnavailableError(LLMError):
    pass


@dataclass
class Config:
    base_url: str = ""
    api_key: str = ""
    vision_model: str = ""
    text_model: str = ""
    embedding_model: str = ""
    timeout: int = 60
    max_retries: int = 3
    offline: bool = False
    grounding_threshold: float = 0.42
    confidence_review: float = 0.60
    max_practice_per_day: int = 3
    db_path: str = "data/pathfinder.db"
    kb_path: str = "knowledge_base/knowledge_points.json"
    matchers_path: str = "knowledge_base/matchers.json"

    @classmethod
    def from_env(cls) -> "Config":
        local = dotenv_values(Path(__file__).with_name(".env"))

        def value(name: str, default: str = "") -> str:
            configured = local.get(name)
            return os.getenv(name, default if configured is None else configured).strip()

        def f(name: str, default: float) -> float:
            try:
                number = float(value(name, str(default)))
                return number if math.isfinite(number) else default
            except ValueError:
                return default

        offline = value("PATHFINDER_OFFLINE", "0").lower() in ("1", "true", "yes")
        return cls(
            base_url=value("LLM_BASE_URL"),
            api_key=value("LLM_API_KEY"),
            vision_model=value("VISION_MODEL"),
            text_model=value("TEXT_MODEL"),
            embedding_model=value("EMBEDDING_MODEL"),
            grounding_threshold=f("GROUNDING_THRESHOLD", 0.42),
            confidence_review=f("CONFIDENCE_REVIEW", 0.60),
            max_practice_per_day=max(1, int(f("MAX_PRACTICE_PER_DAY", 3))),
            db_path=value("DB_PATH", "data/pathfinder.db"),
            kb_path=value("KB_PATH", "knowledge_base/knowledge_points.json"),
            matchers_path=value("MATCHERS_PATH", "knowledge_base/matchers.json"),
            offline=offline,
        )

    @property
    def configured(self) -> bool:
        return bool(self.api_key and self.base_url and self.text_model)


def _strip_json_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?", "", text).strip()
        text = re.sub(r"```$", "", text).strip()
    return text


def _tolerant_json(text: str) -> Dict[str, Any]:
    """Accept raw JSON, fenced JSON, or JSON embedded in prose."""
    if not isinstance(text, str):
        raise LLMResponseError("Expected a text response containing a JSON object.")
    cleaned = _strip_json_fences(text)
    try:
        value = json.loads(cleaned)
    except json.JSONDecodeError:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start >= 0 and end > start:
            try:
                value = json.loads(cleaned[start : end + 1])
            except json.JSONDecodeError as exc:
                raise LLMResponseError("Model did not return parseable JSON.") from exc
        else:
            raise LLMResponseError("Model did not return parseable JSON.")
    if not isinstance(value, dict):
        raise LLMResponseError("Expected a JSON object.")
    return value


# --------------------------------------------------------------------------
# OpenAI-compatible provider
# --------------------------------------------------------------------------
class OpenAICompatProvider:
    name = "openai-compatible"

    def __init__(self, cfg: Config):
        self.cfg = cfg

    def _post(self, path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        url = f"{self.cfg.base_url.rstrip('/')}{path}"
        last: Optional[Exception] = None
        for attempt in range(self.cfg.max_retries):
            try:
                resp = requests.post(
                    url,
                    headers={
                        "Authorization": f"Bearer {self.cfg.api_key}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                    timeout=self.cfg.timeout,
                )
            except requests.exceptions.RequestException as exc:  # network layer
                last = exc
                time.sleep(2 ** attempt)
                continue
            if resp.status_code in (401, 403):
                raise LLMAuthError(f"{resp.status_code}: check LLM_API_KEY")
            if resp.status_code == 429:
                if attempt == self.cfg.max_retries - 1:
                    raise LLMQuotaError("429: quota exhausted — check your plan or wait")
                time.sleep(2 ** attempt)
                continue
            if resp.status_code >= 500:
                last = LLMNetworkError(f"{resp.status_code}: {resp.text[:200]}")
                time.sleep(2 ** attempt)
                continue
            if resp.status_code >= 400:
                raise LLMResponseError(f"{resp.status_code}: {resp.text[:200]}")
            try:
                return resp.json()
            except ValueError as exc:
                raise LLMResponseError(f"malformed JSON body: {resp.text[:200]}") from exc
        raise LLMNetworkError(f"request failed after {self.cfg.max_retries} attempts: {last}")

    def chat(self, system: str, user: str) -> str:
        body = self._post(
            "/chat/completions",
            {
                "model": self.cfg.text_model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "temperature": 0,
            },
        )
        try:
            return body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMResponseError(f"unexpected chat response shape: {str(body)[:200]}") from exc

    def parse_photo(self, image_path: str) -> Dict[str, Any]:
        if not self.cfg.vision_model:
            raise VisionUnavailableError("VISION_MODEL is not set — photo parsing unavailable")
        with open(image_path, "rb") as fh:
            b64 = base64.b64encode(fh.read()).decode("ascii")
        mime = mimetypes.guess_type(image_path)[0] or "image/jpeg"
        body = self._post(
            "/chat/completions",
            {
                "model": self.cfg.vision_model,
                "messages": [
                    {"role": "system", "content": VISION_SYSTEM},
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "Transcribe this exercise exactly as written."},
                            {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
                        ],
                    },
                ],
                "temperature": 0,
            },
        )
        try:
            content = body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMResponseError(f"unexpected vision response shape: {str(body)[:200]}") from exc
        return _tolerant_json(content)

    def diagnose(self, question: str, steps: List[str]) -> Dict[str, Any]:
        payload = json.dumps({"question": question, "steps": steps}, ensure_ascii=False)
        return _tolerant_json(self.chat(DIAGNOSE_SYSTEM, payload))

    def embed(self, texts: Iterable[str]) -> List[List[float]]:
        texts = list(texts)
        if not self.cfg.embedding_model:
            raise LLMNotConfiguredError("EMBEDDING_MODEL is not set")
        body = self._post("/embeddings", {"model": self.cfg.embedding_model, "input": texts})
        try:
            data = body["data"]
            if any("index" in row for row in data):
                data = sorted(data, key=lambda row: row["index"])
                if [row["index"] for row in data] != list(range(len(texts))):
                    raise ValueError("Invalid embedding indexes")
            vectors = [row["embedding"] for row in data]
            if len(vectors) != len(texts) or any(
                not isinstance(vec, list) or not vec
                or not all(isinstance(n, (int, float)) and math.isfinite(n) for n in vec)
                for vec in vectors
            ) or len({len(vec) for vec in vectors}) > 1:
                raise ValueError("Invalid embedding vectors")
            return vectors
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise LLMResponseError(f"unexpected embeddings response shape: {str(body)[:200]}") from exc


# --------------------------------------------------------------------------
# Offline rule baseline
# --------------------------------------------------------------------------
@dataclass
class Misconception:
    name: str
    pattern: str
    error_type: str
    knowledge_point: str
    confidence: float = 0.9
    blame: str = "matching"  # matching → the step that matches; first → the student's first transformation
    explanation: str = ""
    validate: Optional[Callable[[re.Match], bool]] = None

    def matches(self, text: str) -> bool:
        match = re.search(self.pattern, text, flags=re.IGNORECASE)
        return match is not None and (self.validate is None or self.validate(match))


def _added_denominators(match: re.Match) -> bool:
    a, b, c, d, numerator, denominator = map(int, match.groups())
    return (
        b > 0 and d > 0 and b != d
        and numerator == a + c and denominator == b + d
        and Fraction(a, b) + Fraction(c, d) != Fraction(numerator, denominator)
    )


MISCONCEPTIONS: List[Misconception] = [
    Misconception(
        "distributive_drop",
        r"\b([2-9]|[1-9]\d+)\s*\(\s*([a-z])\s*([+-])\s*([1-9]\d*)\s*\)[^;]*;\s*\1\s*\2\s*\3\s*\4\s*=",
        "conceptual", "ALG-7.3", blame="first",
        explanation="括号外的系数乘进了含未知数的一项，也要乘进常数项。请检查去括号后的常数。",
    ),
    Misconception(
        "square_of_sum",
        r"\(\s*([a-z])\s*[+-]\s*([a-z])\s*\)\s*\^?\s*2\s*=\s*\1\s*\^?\s*2\s*\+\s*\2\s*\^?\s*2\s*$",
        "conceptual", "ALG-8.3",
        explanation="平方展开时遗漏了两个交叉相乘项。把平方写成两个相同括号相乘，再逐项展开。",
    ),
    Misconception(
        "fraction_add_across",
        r"^\s*(\d{1,12})\s*/\s*(\d{1,12})\s*\+\s*(\d{1,12})\s*/\s*(\d{1,12})\s*=\s*(\d{1,12})\s*/\s*(\d{1,12})\s*$",
        "conceptual", "ALG-7.4", validate=_added_denominators,
        explanation="这里分别相加了分子和分母。先把两个分数通分，分母保持相同，再相加分子。",
    ),
    Misconception(
        "sign_drop_transpose",
        r"([a-z])\s*-\s*(\d+)\s*=\s*(\d+)[^;]*;\s*\1\s*=\s*\3\s*-\s*\2\s*$",
        "careless", "ALG-7.2", 0.85, "first",
        explanation="消去等号左边的减项，需要在等式两边都加上同一个数。检查移项后的符号。",
    ),
    Misconception(
        "unit_not_converted", r"\b(\d+(?:\.\d+)?)\s*g\b[^;]*;[^;]*/\s*\1(?=\s|$)",
        "procedural", "PHY-9.2", 0.85, "first",
        explanation="代入公式时仍使用了克数。先把质量换算成千克，再用力除以质量。",
    ),
    Misconception(
        "unbalanced_equation", r"^\s*H2\s*\+\s*O2\s*(?:->|→)\s*H2O\s*$",
        "procedural", "CHE-8.1",
        explanation="反应式两边的氧原子数量不一致。调整化学式前的系数，再逐种元素核对。",
    ),
]


class StubProvider:
    """Deterministic rule baseline. No network, no model — labelled in every output."""

    name = "offline-rule-baseline"

    def __init__(self, cfg: Config):
        self.cfg = cfg

    def parse_photo(self, image_path: str) -> Dict[str, Any]:
        raise VisionUnavailableError(
            "offline mode has no vision provider — pass --question/--steps or use a sample from samples/"
        )

    def diagnose(self, question: str, steps: List[str]) -> Dict[str, Any]:
        if not question and not steps:
            return {
                "broken_step": None,
                "error_type": "unreadable",
                "knowledge_point_hint": "",
                "confidence": 1.0,
                "explanation": "The work could not be read. Please upload a clearer, straight-on photo.",
            }
        for i, written_step in enumerate(steps):
            for rule in MISCONCEPTIONS:
                if rule.blame == "first" and i != 0:
                    continue
                evidence = f"{question} ; {written_step}" if rule.blame == "first" else written_step
                if rule.matches(evidence):
                    return {
                        "broken_step": written_step,
                        "error_type": rule.error_type,
                        "knowledge_point_hint": rule.knowledge_point,
                        "confidence": rule.confidence,
                        "explanation": rule.explanation,
                    }
        return {
            "broken_step": None,
            "error_type": "procedural",
            "knowledge_point_hint": "",
            "confidence": 0.35,
            "explanation": "No known misconception matched. A teacher should check the working.",
        }

    def embed(self, texts: Iterable[str]) -> List[List[float]]:
        """Hashed bag-of-words vectors — deterministic, no network, unit-normalised."""
        dim = 256
        out: List[List[float]] = []
        for text in texts:
            vec = [0.0] * dim
            for token in re.findall(r"[a-z0-9]+", str(text).lower()):
                vec[int(hashlib.md5(token.encode()).hexdigest(), 16) % dim] += 1.0
            norm = math.sqrt(sum(v * v for v in vec)) or 1.0
            out.append([v / norm for v in vec])
        return out


def get_provider(cfg: Optional[Config] = None, force_offline: bool = False) -> Any:
    cfg = cfg or Config.from_env()
    if force_offline or cfg.offline or not cfg.configured:
        return StubProvider(cfg)
    return OpenAICompatProvider(cfg)
