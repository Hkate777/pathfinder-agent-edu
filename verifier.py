"""Step-level verification with SymPy — deterministic evidence, never guessed.

Role in the honesty contract (ADOPTION_NOTES.md §2.2, assessment P0-6/P1-6):
the CAS gives objective per-step verdicts for a student's working. Where SymPy
cannot parse or cannot decide, the verdict is ``unknown`` — it is never
fabricated. This module does NOT replace the LLM diagnosis; it is independent
evidence that phase-1 work may combine with other review signals.

Coverage (v0.1, honest about limits):
  * single-variable linear equations — a step is ``pass`` iff it has the same
    solution set as the question's constraint (or its value satisfies it);
  * variable-free arithmetic equalities — ``pass`` iff both sides are equal;
  * everything else (multi-variable, inequalities, word problems, chemistry…)
    is ``unknown``.

CLI:
    python verifier.py --question "Solve 2(x+3)=4x-6" --steps "2x+3=4x-6;x=4.5"
    python verifier.py --sample samples/alg_distributive.json --json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from sympy import Eq, solve, simplify
from sympy.parsing.sympy_parser import (
    implicit_multiplication_application,
    parse_expr,
    rationalize,
    standard_transformations,
    convert_xor,
)

# Characters students actually write on paper but SymPy cannot parse.
_CHAR_MAP = {
    "\u2212": "-",  # minus sign
    "\u00d7": "*",  # multiplication sign
    "\u00f7": "/",  # division sign
    "\uff0b": "+",  # fullwidth plus
    "\uff0d": "-",  # fullwidth minus
    "\uff1d": "=",  # fullwidth equals
    "\uff08": "(",  # fullwidth(
    "\uff09": ")",  # fullwidth )
    "\u2260": "!=",  # not-equal (still unparseable, but normalised)
    "\u3002": ".",  # CJK full stop
    "\uff0c": ",",  # fullwidth comma
}
_CJK_RE = re.compile(r"[\u4e00-\u9fff]+")
_TRANSFORMS = standard_transformations + (convert_xor, implicit_multiplication_application, rationalize)


def normalize(text: str) -> str:
    """Unicode / typography normalisation so CAS parsing is not blocked by pen strokes."""
    if not text:
        return ""
    out = unicodedata.normalize("NFKC", text)
    for src, dst in _CHAR_MAP.items():
        out = out.replace(src, dst)
    out = _CJK_RE.sub(" ", out)  # strip CJK prose, keep the math
    # Drop multi-letter English words ("Solve", "Find"). Without this, implicit
    # multiplication turns prose into symbol products (e*l*o*v*x) that parse
    # "successfully" and poison the constraint. v0.1 variables are single-letter;
    # losing named functions (sin, sqrt…) makes those steps `unknown` — honest.
    out = re.sub(r"\b[A-Za-z]{2,}\b", " ", out)
    return re.sub(r"\s+", " ", out).strip()


def _split_equation(text: str) -> Optional[tuple[str, str]]:
    """Split on the first '=' (ignore ==). Inequalities are not covered -> None."""
    if any(op in text for op in ("<", ">", "!=")):
        return None
    parts = text.split("=")
    if len(parts) != 2:
        return None
    lhs, rhs = parts[0].strip(), parts[1].strip()
    return (lhs, rhs) if lhs and rhs else None


def _parse(side: str):
    """Parse one side of an equation; returns a SymPy expression or None."""
    try:
        expr = parse_expr(side, transformations=_TRANSFORMS, evaluate=True)
        return expr if expr.free_symbols is not None else None
    except Exception:  # noqa: BLE001 - any parse failure means "unknown", never a guess
        return None


def _longest_parseable_suffix(text: str):
    """Parse the longest trailing token run that forms a valid expression.

    Question text usually wraps the equation in prose ("Find the value of x:
    2(x+3) = 4x - 6"). Trying suffixes longest-first recovers the math without
    guessing at its meaning.
    """
    tokens = text.split()
    for i in range(len(tokens)):
        expr = _parse(" ".join(tokens[i:]))
        if expr is not None:
            return expr
    return None


@dataclass
class StepVerdict:
    index: int
    step: str
    verdict: str  # "pass" | "fail" | "unknown"
    reason: str


@dataclass
class WorkVerdict:
    question: str
    constraint: Optional[str]
    verdicts: List[StepVerdict] = field(default_factory=list)

    @property
    def first_fail(self) -> Optional[int]:
        for v in self.verdicts:
            if v.verdict == "fail":
                return v.index
        return None

    @property
    def decided(self) -> int:
        return sum(1 for v in self.verdicts if v.verdict != "unknown")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "question": self.question,
            "constraint": self.constraint,
            "first_fail": self.first_fail,
            "decided": self.decided,
            "total": len(self.verdicts),
            "verdicts": [vars(v) for v in self.verdicts],
        }


def _symbols_of(expr) -> set:
    try:
        return {s for s in expr.free_symbols}
    except Exception:  # noqa: BLE001
        return set()


def _same_solutions(eq_a: Eq, eq_b: Eq, symbol) -> Optional[bool]:
    """True/False when both sides solve cleanly; None when undecidable."""
    try:
        sol_a = solve(eq_a, symbol)
        sol_b = solve(eq_b, symbol)
        if not sol_a and not sol_b:
            return True  # both degenerate (e.g. identity) — treat as consistent
        norm = lambda sols: {simplify(s) for s in sols}
        return norm(sol_a) == norm(sol_b)
    except Exception:  # noqa: BLE001
        return None


def _verify_constraint_step(step_expr, constraint: Eq, symbols: set) -> tuple[str, str]:
    """A step with variables: same solution set as the constraint, or satisfies it."""
    step_symbols = _symbols_of(step_expr.lhs) | _symbols_of(step_expr.rhs)
    target = step_symbols & symbols or symbols
    if len(step_symbols) > 1:
        return "unknown", "multi-variable step not covered by v0.1"
    if not step_symbols:
        # Numeric claim like "6 = 6": check arithmetic equality directly.
        ok = simplify(step_expr.lhs - step_expr.rhs) == 0
        return ("pass", "arithmetic identity") if ok else ("fail", "numeric sides differ")
    symbol = sorted(target, key=str)[0]
    same = _same_solutions(step_expr, constraint, symbol)
    if same is None:
        return "unknown", "solver could not decide"
    if same:
        return "pass", "same solution set as the question"
    return "fail", "solution set differs from the question"


def verify_work(question: str, steps: List[str]) -> WorkVerdict:
    """Verify each step of a student's working. Unknowns are reported, not guessed."""
    work = WorkVerdict(question=question, constraint=None)

    q_norm = normalize(question)
    q_parts = _split_equation(q_norm) if q_norm else None
    # If the question itself signals a mathematical goal (any '=' — including
    # multi-equation text we cannot split — or an inequality) but we failed to
    # build a usable constraint, adopting a student step as the reference would
    # self-certify a wrong working — report unknown instead.
    has_question_math = any(op in q_norm for op in ("=", "<", ">", "!="))
    constraint: Optional[Eq] = None
    constraint_symbols: set = set()
    if q_parts:
        # Try the raw sides first, then the longest parseable suffix — question
        # prose ("Solve …", "Find the value of x: …") must not disable the
        # constraint, otherwise a wrong first step would become the reference.
        lhs = _parse(q_parts[0])
        if lhs is None:
            lhs = _longest_parseable_suffix(q_parts[0])
        rhs = _parse(q_parts[1])
        if rhs is None:
            rhs = _longest_parseable_suffix(q_parts[1])
        if lhs is not None and rhs is not None:
            eq = Eq(lhs, rhs)
            syms = _symbols_of(lhs) | _symbols_of(rhs)
            if eq is not None and len(syms) == 1:
                constraint, constraint_symbols = eq, syms
                work.constraint = q_norm

    for i, raw in enumerate(steps):
        step_norm = normalize(raw)
        if not step_norm:
            work.verdicts.append(StepVerdict(i, raw, "unknown", "empty step"))
            continue
        parts = _split_equation(step_norm)
        if parts is None:
            work.verdicts.append(StepVerdict(i, raw, "unknown", "not a simple equation (or unsupported operator)"))
            continue
        lhs, rhs = _parse(parts[0]), _parse(parts[1])
        if lhs is None or rhs is None:
            work.verdicts.append(StepVerdict(i, raw, "unknown", "parse failure"))
            continue
        eq = Eq(lhs, rhs)
        step_symbols = _symbols_of(lhs) | _symbols_of(rhs)

        if constraint is not None:
            verdict, reason = _verify_constraint_step(eq, constraint, constraint_symbols)
            if verdict == "unknown" and not step_symbols and not (_symbols_of(constraint.lhs) | _symbols_of(constraint.rhs)):
                # numeric identity with a numeric question: pure arithmetic
                ok = simplify(lhs - rhs) == 0
                verdict, reason = (("pass", "arithmetic identity") if ok else ("fail", "numeric sides differ"))
            work.verdicts.append(StepVerdict(i, raw, verdict, reason))
            continue

        # No constraint from the question. If the question gave no mathematical
        # signal at all AND the first variable equation parses cleanly, adopt it
        # as the working constraint (later steps are checked against it);
        # variable-free steps are judged as arithmetic identities.
        if not work.constraint and not has_question_math and step_symbols and len(step_symbols) == 1:
            work.constraint = step_norm
            constraint = eq
            constraint_symbols = step_symbols
            work.verdicts.append(StepVerdict(i, raw, "pass", "adopted as the working constraint"))
            continue
        if not step_symbols:
            ok = simplify(lhs - rhs) == 0
            work.verdicts.append(StepVerdict(i, raw, "pass" if ok else "fail",
                                             "arithmetic identity" if ok else "numeric sides differ"))
            continue
        if not has_question_math:
            # Identity-style questions ("Expand (a+b)^2"): the step must hold
            # identically. With a question signal but no usable constraint
            # (inequalities, multi-variable systems) we cannot judge — unknown.
            ok = simplify(lhs - rhs) == 0
            work.verdicts.append(
                StepVerdict(i, raw, "pass" if ok else "fail",
                            "identity holds (no question constraint)" if ok
                            else "sides are not identical (identity check)")
            )
            continue
        work.verdicts.append(StepVerdict(i, raw, "unknown",
                                         "question states a goal we cannot model (inequality / multi-variable)"))

    return work


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Verify student working step-by-step with SymPy (offline, deterministic).")
    ap.add_argument("--question", help="the question text")
    ap.add_argument("--steps", help="steps separated by ';'")
    ap.add_argument("--sample", help="sample JSON: {question, steps[]}")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args(argv)

    if args.sample:
        with open(args.sample, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        question, steps = data.get("question", ""), data.get("steps", [])
    elif args.steps:
        question = args.question or ""
        steps = [s.strip() for s in args.steps.split(";") if s.strip()]
    else:
        ap.error("provide --sample or --question/--steps")
        return 2

    work = verify_work(question, steps)
    if args.json:
        print(json.dumps(work.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(f"[cas-verifier] constraint = {work.constraint or '-'}")
        for v in work.verdicts:
            mark = {"pass": "PASS", "fail": "FAIL", "unknown": "...."}[v.verdict]
            print(f"  step {v.index}: {mark}  {v.step}  ({v.reason})")
        ff = work.first_fail
        print(f"  first fail: {ff if ff is not None else 'none'}  "
              f"decided {work.decided}/{len(work.verdicts)} steps")
    return 0


if __name__ == "__main__":
    sys.exit(main())
