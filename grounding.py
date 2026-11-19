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
        # Matchers pair with LOCAL embeddings. Hashing keeps the blended score
        # (0.75 ratio + 0.25 sim) so the offline baseline stays reproducible.
        # Sentence-transformers adds semantics the regex cannot see (Chinese
        # questions); there we take max(sim, blended) — either a strong regex
        # hit or a strong semantic match is enough, and negative controls stay
        # low on both. Real API embeddings score on cosine alone.
        self.embedding_kind = getattr(provider, "embedding_kind", "")
        self.use_matchers = (
            self.embedding_kind in ("local-hash", "local-st")
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

    def score(self, text: str, node: Dict[str, Any], vec: List[float], text_vec: List[float],
              *, matchers_active: bool = True) -> float:
        sim = cosine(text_vec, vec)
        if not self.use_matchers:
            return round(sim, 4)
        if not matchers_active:
            # Two-stage rule for semantic embeddings (see ground()): with no
            # syllabus matcher firing anywhere, fall back to pure semantics so
            # Chinese questions can still ground.
            return round(sim, 4)
        ratio = self.matcher_ratio(text, node["id"])
        return round(0.75 * ratio + 0.25 * sim, 4)

    def ground(self, text: str, hint: str = "") -> GroundResult:
        method = "matchers+embeddings" if self.use_matchers else "embeddings"
        if not self.nodes:
            return GroundResult(None, 0.0, True, self.threshold, method)
        if self.vectors is None:
            self.vectors = self.provider.embed(self.texts)
        # Hint handling is route-dependent, by design:
        #  * hashed baseline — the hint comes from the same rule engine and acts
        #    as an extra evidence token; it keeps the rule route reproducible.
        #  * semantic model — a model guess must not steer retrieval: measured,
        #    a wrong PHY-9.1 hint dragged a unit-conversion case off PHY-9.2
        #    (confirmation bias, review §6.2 C3).
        query = f"{text}\n{hint}".strip() if self.embedding_kind == "local-hash" else text
        query_vec = self.provider.embed([query])[0]
        # With a semantic model, matcher evidence must dominate when present:
        # ST similarity alone ranks "same topic" too broadly (e.g. prefers
        # Newton's law over unit conversion because the question mentions
        # force). Only when NO matcher fires at all do we trust pure semantics.
        ratios = {node["id"]: self.matcher_ratio(query, node["id"]) for node in self.nodes}
        matchers_active = not (self.embedding_kind == "local-st" and max(ratios.values(), default=0) == 0)
        scored = [
            (self.score(query, node, self.vectors[i], query_vec, matchers_active=matchers_active), node)
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
