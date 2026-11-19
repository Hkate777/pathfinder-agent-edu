"""Check transcript-to-diagnosis behaviour using demo or authored challenge cases.

Use --set samples, --set parsing (provided parse statuses), or --set challenge.
Offline is the default. --online runs only the explicitly configured text and
embedding services. No suite measures photo transcription or student outcomes.
"""

from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import sys
from typing import Any, Dict, List

from diagnose import Pipeline
from llm import Config
from store import utc_now
from vision import from_sample

ROOT = Path(__file__).resolve().parent
SUITE_NOTES = {
    "samples": "Repository demo transcripts with fixture labels. Rules were developed using these examples.",
    "parsing": "Routing of provided non-ok parse statuses. No photos are processed.",
    "challenge": "Separately authored synthetic classroom transcripts with author-specified references. "
                 "They have no independent teacher validation.",
}


def load_samples() -> List[Dict[str, Any]]:
    """The Streamlit sample picker and the original fixture evaluation share these cases."""
    out = []
    for path in sorted((ROOT / "samples").glob("*.json")):
        sample = json.loads(path.read_text(encoding="utf-8"))
        sample["_path"] = str(path.relative_to(ROOT))
        out.append(sample)
    return out


def suite_samples(suite: str) -> list[dict]:
    if suite == "challenge":
        return json.loads((ROOT / "evaluation" / "classroom_challenge.json").read_text(encoding="utf-8"))["cases"]
    samples = load_samples()
    if suite == "parsing":
        return [case for case in samples if not from_sample(case).ok]
    return samples


def reference_step(sample: dict, kind: str) -> str | None:
    if kind != "error":
        return None
    expected = sample["expected"]
    steps = sample["steps"]
    number = expected.get("step_number")
    if number is not None:
        if not isinstance(number, int) or not 1 <= number <= len(steps):
            raise ValueError(f"Invalid reference step number in {sample['id']}")
        return steps[number - 1]
    fragment = expected.get("broken_step_contains")
    matches = [step for step in steps if fragment and fragment in step]
    if len(matches) != 1:
        raise ValueError(f"Reference fragment must identify exactly one step in {sample['id']}")
    return matches[0]


# For a correct or incomplete transcript, teacher review is a safe disposition.
# It does not earn credit for recognising correctness or locating an error.
ACCEPTED_ROUTES = {
    "error": {"diagnosis"},
    "correct": {"correct", "review"},
    "insufficient": {"clarify", "review"},
    "retake": {"retake"},
}


def result_route(result: dict) -> str:
    status = result["status"]
    if status in {"ambiguous", "unreadable", "rotated"}:
        return "retake"
    if status != "ok":
        return status
    flags = result["flags"]
    if flags["needs_teacher_review"] or flags["unmapped"]:
        return "review"
    return "diagnosis"


def score_one(pipe: Pipeline, sample: Dict[str, Any]) -> Dict[str, Any]:
    parsed = from_sample(sample)
    expected = sample["expected"]
    kind = expected.get("kind", "error" if parsed.ok else "retake")
    reference = reference_step(sample, kind)
    result = pipe.run(parsed, student_id=f"eval-{sample['id']}", save=False)
    route = result_route(result)
    broken = result.get("broken_step") or None
    return {
        "sample": sample["id"],
        "reference_kind": kind,
        "route": route,
        "route_ok": route in ACCEPTED_ROUTES[kind],
        "expected_type": expected.get("error_type"),
        "got_type": result.get("error_type"),
        "type_ok": (broken is not None and expected["error_type"] == result.get("error_type"))
                   if kind == "error" else None,
        "expected_kp": expected.get("knowledge_point"),
        "got_kp": result.get("knowledge_point"),
        "kp_ok": expected["knowledge_point"] == result.get("knowledge_point") if kind == "error" else None,
        "expected_step": reference,
        "got_step": broken,
        "broken_step_ok": broken == reference if kind == "error" else None,
        "wrong_claim": broken != reference if broken is not None else None,
        "false_blame": broken is not None if kind in {"correct", "insufficient"} else None,
        "confidence": result.get("confidence"),
        "unmapped": result["flags"]["unmapped"],
        "review": route == "review",
    }


METRICS = {
    "type_ok": "Error type with a cited step / labelled errors",
    "kp_ok": "Knowledge-point match / labelled errors",
    "broken_step_ok": "First broken step located / labelled errors",
    "route_ok": "Expected routing / cases",
    "wrong_claim": "Wrongly attributed steps / cases with a step claim",
    "false_blame": "Step blamed / correct or insufficient cases",
}


def metric_counts(rows: list[dict]) -> dict[str, tuple[int, int]]:
    counts = {}
    for key in METRICS:
        applicable = [row[key] for row in rows if row[key] is not None]
        counts[key] = (sum(applicable), len(applicable))
    return counts


def main(argv: List[str] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--set", default="samples", choices=SUITE_NOTES)
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--offline", action="store_true", help="use the deterministic baseline (default)")
    mode.add_argument("--online", action="store_true", help="run the configured text and embedding models")
    ap.add_argument("--save", action="store_true", help="write the report to traces/")
    args = ap.parse_args(argv)

    cfg = Config.from_env()
    if args.online and (cfg.offline or not cfg.configured):
        ap.error("--online needs a configured model and PATHFINDER_OFFLINE=0")
    samples = suite_samples(args.set)
    if not samples:
        ap.error("No cases available for the requested set.")
    pipe = Pipeline(cfg, force_offline=not args.online)
    rows = [score_one(pipe, sample) for sample in samples]
    summary = [f"Set = {args.set}", f"Provider = {pipe.provider.name}", SUITE_NOTES[args.set], f"Cases = {len(rows)}"]
    print("\n".join(summary))
    print(f"{'Sample':<31} {'Reference':<14} {'Route':<13} {'Step':<8}")
    for row in rows:
        step_label = {None: "n/a", True: "hit", False: "miss"}[row["broken_step_ok"]]
        print(f"{row['sample']:<31} {row['reference_kind']:<14} {row['route']:<13} {step_label:<8}")

    routes = Counter(row["route"] for row in rows)
    metrics = [f"Routes = {', '.join(f'{route} {n}' for route, n in sorted(routes.items()))}"]
    for key, (hits, n) in metric_counts(rows).items():
        value = f"{hits}/{n} ({hits / n:.0%})" if n else "n/a (0 eligible)"
        metrics.append(f"{METRICS[key]} = {value}")
    print("\n".join(metrics))

    if args.save:
        directory = ROOT / "traces"
        directory.mkdir(exist_ok=True)
        path = directory / f"eval_{args.set}_{utc_now():%Y%m%d_%H%M%S_%f}.md"
        report = "\n".join(f"- {line}" for line in [*summary, *metrics])
        details = json.dumps(rows, ensure_ascii=False, indent=2)
        path.write_text(f"# Evaluation report\n\n{report}\n\n```json\n{details}\n```\n", encoding="utf-8")
        print(f"Report = {path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
