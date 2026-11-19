"""Curriculum grounding: retrieve the knowledge point, or report it unmapped — never guess."""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional


@dataclass
class GroundResult:
    node: Optional[Dict[str, Any]]
    score: float
    unmapped: bool
    threshold: float
    method: str

    @property
    def knowledge_point(self) -> Optional[str]:
        return self.node["id"] if self.node else None


def cosine(a: List[float], b: List[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    return max(0.0, dot / (na * nb))


class Grounder:
    """Blends regex matchers (syllabus-shaped signal) with embedding similarity."""

    def __init__(self, nodes: List[Dict[str, Any]], matchers: Dict[str, List[str]], provider, threshold: float = 0.42, use_matchers: Optional[bool] = None):
        self.nodes = nodes
        self.matchers = matchers
        self.provider = provider
        self.threshold = threshold
        # Matchers pair with the local hashed embedding: offline baseline and the
        # hybrid (online chat + local embed) route use the same blended score, so
        # only the diagnosis model changes between the two runs. Real API
        # embeddings score on cosine alone (matchers would cap them at 0.25).
        self.use_matchers = (
            getattr(provider, "embedding_kind", "") == "local-hash"
        ) if use_matchers is None else use_matchers
        self.texts = [self.node_text(n) for n in nodes]
        self.vectors: Optional[List[List[float]]] = None

    @staticmethod
    def node_text(node: Dict[str, Any]) -> str:
        parts = [node.get("title", ""), " ".join(node.get("keywords", [])), node.get("remediation", "")]
        parts += [item.get("text", "") for item in node.get("items", [])]
        return " ".join(parts)

    def matcher_ratio(self, text: str, node_id: str) -> float:
        patterns = self.matchers.get(node_id, [])
        if not patterns:
            return 0.0
        hits = sum(1 for p in patterns if re.search(p, text, flags=re.IGNORECASE))
        return hits / len(patterns)

    def score(self, text: str, node: Dict[str, Any], vec: List[float], text_vec: List[float]) -> float:
        sim = cosine(text_vec, vec)
        if not self.use_matchers:
            return round(sim, 4)
        ratio = self.matcher_ratio(text, node["id"])
        return round(0.75 * ratio + 0.25 * sim, 4)

    def ground(self, text: str, hint: str = "") -> GroundResult:
        method = "matchers+embeddings" if self.use_matchers else "embeddings"
        if not self.nodes:
            return GroundResult(None, 0.0, True, self.threshold, method)
        if self.vectors is None:
            self.vectors = self.provider.embed(self.texts)
        query = f"{text}\n{hint}".strip()
        query_vec = self.provider.embed([query])[0]
        scored = [
            (self.score(query, node, self.vectors[i], query_vec), node)
            for i, node in enumerate(self.nodes)
        ]
        scored.sort(key=lambda pair: pair[0], reverse=True)
        best_score, best_node = scored[0]
        return GroundResult(
            node=best_node if best_score >= self.threshold else None,
            score=best_score,
            unmapped=best_score < self.threshold,
            threshold=self.threshold,
            method=method,
        )


def load_kb(kb_path: str, matchers_path: str):
    with open(kb_path, "r", encoding="utf-8") as fh:
        nodes = json.load(fh)["nodes"]
    with open(matchers_path, "r", encoding="utf-8") as fh:
        matchers = json.load(fh)["matchers"]
    return nodes, matchers
