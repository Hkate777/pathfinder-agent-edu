"""Data isolation gate: dev ∩ held_out = ∅, plus frozen-file protection.

Implements the hard rules from evaluation/README.md:
  1. no text overlap between dev/challenge/KB practice items and held_out.json
  2. once frozen (held_out.hash exists), held_out.json must not change
  3. exit code 0 = gate passed; 1 = violation; held_out missing = not yet active

Usage:
    python scripts/check_data_isolation.py            # run the gate
    python scripts/check_data_isolation.py --freeze   # one-time freeze
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HOLD_OUT = ROOT / "evaluation" / "held_out.json"
HOLD_OUT_HASH = ROOT / "evaluation" / "held_out.hash"
KB = ROOT / "knowledge_base" / "knowledge_points.json"


def normalize_text(text: str) -> str:
    """Canonical form for overlap comparison: NFKC + strip all whitespace + lower."""
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text or "")).lower()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def collect_texts(path: Path) -> list[tuple[str, str]]:
    """Return [(case_id, normalized question+steps)] for overlap checks."""
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    cases = data.get("cases", []) if isinstance(data, dict) else data
    out = []
    for case in cases:
        text = normalize_text(case.get("question", "")) + \
            normalize_text("".join(case.get("steps", []) or []))
        if text:
            out.append((case.get("id", "?"), text))
    return out


def collect_questions(path: Path) -> list[tuple[str, str]]:
    """Return [(case_id, normalized question only)] — used for KB leakage checks,
    because KB practice items are compared by question text alone."""
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    cases = data.get("cases", []) if isinstance(data, dict) else data
    out = []
    for case in cases:
        text = normalize_text(case.get("question", ""))
        if text:
            out.append((case.get("id", "?"), text))
    return out


def dev_texts() -> list[tuple[str, str]]:
    """Dev set = samples/ (explicitly the leakage channel flagged in review)."""
    texts = []
    for path in sorted((ROOT / "samples").glob("*.json")):
        texts.extend(collect_texts(path))
    return texts


def challenge_texts() -> list[tuple[str, str]]:
    return collect_texts(ROOT / "evaluation" / "classroom_challenge.json")


def kb_practice_texts() -> list[tuple[str, str]]:
    """Practice items inside the knowledge base must not leak into held_out."""
    if not KB.exists():
        return []
    data = json.loads(KB.read_text(encoding="utf-8"))
    nodes = data.get("nodes", []) if isinstance(data, dict) else data
    out = []
    for node in nodes:
        for item in node.get("items", []):
            text = normalize_text(item.get("question", ""))
            if text:
                out.append((item.get("id", "?"), text))
    return out


def overlaps(source: list[tuple[str, str]], held: dict[str, str], label: str) -> list[str]:
    problems = []
    for case_id, text in source:
        if text in held:
            problems.append(f"  OVERLAP: {label} case '{case_id}' also appears in held_out as '{held[text]}'")
    return problems


def run_gate(freeze: bool = False) -> int:
    if not HOLD_OUT.exists():
        print("[isolation] evaluation/held_out.json not created yet — gate inactive (exit 0).")
        return 0

    raw = HOLD_OUT.read_bytes()
    digest = sha256_bytes(raw)

    if freeze:
        HOLD_OUT_HASH.write_text(digest, encoding="utf-8")
        print(f"[isolation] FROZEN: evaluation/held_out.hash = {digest[:16]}…  (do not edit held_out.json after this)")
        return 0

    if HOLD_OUT_HASH.exists():
        recorded = HOLD_OUT_HASH.read_text(encoding="utf-8").strip()
        if recorded != digest:
            print(f"[isolation] FAIL: held_out.json changed after freeze "
                  f"(recorded {recorded[:16]}…, current {digest[:16]}…). "
                  "A frozen hold-out must never be edited.")
            return 1

    held = {text: cid for cid, text in collect_texts(HOLD_OUT)}
    held_questions = {text: cid for cid, text in collect_questions(HOLD_OUT)}
    problems: list[str] = []
    problems += overlaps(dev_texts(), held, "dev")
    problems += overlaps(challenge_texts(), held, "challenge")
    problems += overlaps(kb_practice_texts(), held_questions, "kb-practice")

    if problems:
        print(f"[isolation] FAIL: {len(problems)} overlap(s) with held_out.json:")
        print("\n".join(sorted(set(problems))))
        return 1

    n_held = len(held)
    status = "frozen" if HOLD_OUT_HASH.exists() else "unfrozen"
    print(f"[isolation] PASS: no overlap between dev/challenge/kb-practice and held_out.json "
          f"({n_held} held-out cases, {status}).")
    if n_held < 100:
        print(f"[isolation] NOTE: held-out has {n_held} cases; the ≥100 target applies before any external claim.")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--freeze", action="store_true",
                    help="one-time freeze: write evaluation/held_out.hash")
    args = ap.parse_args(argv)
    return run_gate(freeze=args.freeze)


if __name__ == "__main__":
    sys.exit(main())
