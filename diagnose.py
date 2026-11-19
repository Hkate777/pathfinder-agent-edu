"""Core pipeline: parse → diagnose → ground → prescribe → schedule → store.

Also the CLI adapter:
    python diagnose.py --sample samples/alg_distributive.json --trace
    python diagnose.py --question "Solve x - 4 = 10" --steps "x = 10 - 4;x = 6"
    python diagnose.py --image photo.jpg --trace        # needs a vision provider
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import sys
import time
from contextlib import closing
from typing import Any, Dict, List, Optional

from grounding import Grounder, load_kb
from llm import Config, ERROR_TYPES, LLMResponseError, get_provider
from schedule import Gap, build_path, initial_path
from store import (
    add_mistake, completed_practice, connect, find_submission, gap_profile, init_db,
    mistake_history, parse_timestamp, record_submission, utc_now,
)
from vision import RETAKE_MESSAGE, ParseResult, from_sample, from_text, parse_photo

ROOT = os.path.dirname(os.path.abspath(__file__))
LOG = logging.getLogger(__name__)


class Pipeline:
    def __init__(self, cfg: Optional[Config] = None, provider=None, grounder=None, force_offline: bool = False):
        self.cfg = cfg or Config.from_env()
        self.provider = provider or get_provider(self.cfg, force_offline=force_offline)
        if grounder is None:
            nodes, matchers = load_kb(
                os.path.join(ROOT, self.cfg.kb_path), os.path.join(ROOT, self.cfg.matchers_path)
            )
            self.nodes = nodes
            self.grounder = Grounder(nodes, matchers, self.provider, threshold=self.cfg.grounding_threshold)
        else:
            self.grounder = grounder
            self.nodes = grounder.nodes
        self.trace: List[Dict[str, Any]] = []

    def _step(self, name: str, payload: Any) -> None:
        self.trace.append({"step": name, "at": utc_now().isoformat(timespec="seconds"), "payload": payload})

    def node_by_id(self, kp_id: str) -> Optional[Dict[str, Any]]:
        return next((n for n in self.nodes if n["id"] == kp_id), None)

    def run(
        self,
        parsed: ParseResult,
        student_id: str = "anon",
        image_path: Optional[str] = None,
        save: bool = True,
        *,
        source: Optional[str] = None,
        submission_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Diagnose one transcript.

        ``submission_key`` makes persistence idempotent: a repeated submission
        (e.g. a browser refresh) reuses the first mistake row instead of adding
        a second one, which would distort heatmaps and repeat-error metrics.
        """
        t0 = time.time()
        self.trace = []
        self._step("parse", parsed.to_dict())

        if not parsed.ok:
            return self._abstain(parsed, parsed.status, t0)

        raw = self.provider.diagnose(parsed.question, parsed.steps)
        self._step("diagnose", raw)
        error_type = str(raw.get("error_type", "procedural")).lower()
        if error_type == "unreadable":
            return self._abstain(parsed, "unreadable", t0)
        if error_type not in ERROR_TYPES:
            LOG.warning("Provider %s returned an unknown error type. Using procedural.", self.provider.name)
            error_type = "procedural"
        try:
            confidence = float(raw.get("confidence", 0.0) or 0.0)
        except (TypeError, ValueError, OverflowError) as exc:
            raise LLMResponseError("Diagnosis confidence must be a number.") from exc
        if not math.isfinite(confidence) or not 0 <= confidence <= 1:
            raise LLMResponseError("Diagnosis confidence must be between zero and one.")
        quote = raw.get("broken_step")
        broken_step = next(
            (step for step in parsed.steps if step.strip() and isinstance(quote, str) and step.strip() == quote.strip()),
            None,
        )
        explanation = raw.get("explanation") if broken_step else None
        if not isinstance(explanation, str):
            explanation = None
        needs_review = broken_step is None or confidence < self.cfg.confidence_review

        query = " ; ".join([parsed.question] + parsed.steps)
        hint = raw.get("knowledge_point_hint") if broken_step else ""
        ground = self.grounder.ground(query, hint=hint if isinstance(hint, str) else "")
        self._step("ground", {"knowledge_point": ground.knowledge_point, "score": ground.score, "unmapped": ground.unmapped})

        node = ground.node
        items = list(node.get("items", [])) if node else []
        practice = [f"{item['id']} ({item['difficulty']})" for item in items[:3]]

        if not node:
            message = (
                f"No knowledge point in the syllabus matched this work (best score {ground.score} "
                f"< threshold {ground.threshold}). Reported as unmapped rather than guessed — "
                "please tell us the chapter or textbook."
            )
        else:
            message = None

        # Honest failure accounting: unmapped diagnoses are recorded too (grounded=0),
        # so the unmapped share of uploads is measurable instead of invisible.
        mistake_id = None
        duplicate = False
        schedule = initial_path(items)
        plan: List[Dict[str, Any]] = []
        if save:
            with closing(connect(os.path.join(ROOT, self.cfg.db_path))) as conn, conn:
                init_db(conn)
                existing = find_submission(conn, submission_key) if submission_key else None
                if existing is not None:
                    mistake_id = existing
                    duplicate = True
                else:
                    mistake_id = add_mistake(
                        conn,
                        {
                            "student_id": student_id,
                            "image_path": image_path,
                            "question_text": parsed.question,
                            "steps": parsed.steps,
                            "broken_step": broken_step,
                            "error_type": error_type,
                            "knowledge_point_id": node["id"] if node else None,
                            "confidence": confidence,
                            "grounded": bool(node),
                            "provider": self.provider.name,
                        },
                        details={
                            "explanation": explanation,
                            "grounding_score": ground.score,
                            "needs_review": needs_review,
                            "source": source,
                        },
                        commit=False,
                    )
                    if submission_key:
                        record_submission(conn, submission_key, mistake_id)
                plan = self.review_plan(conn, student_id)

        result = {
            "status": "ok",
            "question": parsed.question,
            "steps": parsed.steps,
            "broken_step": broken_step,
            "error_type": error_type,
            "knowledge_point": ground.knowledge_point,
            "confidence": confidence,
            "explanation": explanation,
            "remediation": node.get("remediation") if node else None,
            "practice": practice,
            "schedule": schedule,
            "plan": plan,
            "message": message,
            "mistake_id": mistake_id,
            "flags": {
                "unmapped": ground.unmapped,
                "needs_teacher_review": needs_review,
                "provider": self.provider.name,
                "grounding_score": ground.score,
                "threshold": ground.threshold,
                "duplicate": duplicate,
                "elapsed_s": round(time.time() - t0, 3),
            },
        }
        self._step("result", result)
        return result

    def _abstain(self, parsed: ParseResult, status: str, started: float) -> Dict[str, Any]:
        self._step("abort", {"reason": status, "message": RETAKE_MESSAGE})
        return {
            "status": status,
            "question": parsed.question,
            "steps": parsed.steps,
            "broken_step": None,
            "error_type": "unreadable",
            "knowledge_point": None,
            "confidence": 1.0,
            "explanation": None,
            "practice": [],
            "schedule": [],
            "message": RETAKE_MESSAGE,
            "flags": {
                "unmapped": False,
                "needs_teacher_review": True,
                "provider": self.provider.name,
                "elapsed_s": round(time.time() - started, 3),
            },
        }

    def review_plan(self, conn, student_id: str) -> List[Dict[str, Any]]:
        latest = {}
        for row in mistake_history(conn, student_id, self.cfg.confidence_review):
            if row["decision"] == "rejected":
                continue
            # A teacher-assigned knowledge point (override) makes an otherwise
            # unmapped mistake eligible for practice once the teacher confirms it.
            kp = row["effective_kp"]
            if not kp:
                continue
            if row["needs_review"] and row["decision"] != "confirmed":
                continue
            latest.setdefault(kp, row)
        completed = completed_practice(conn, student_id)
        gaps = []
        for entry in gap_profile(conn, student_id):
            kp = entry["knowledge_point"]
            row = latest.get(kp)
            node = self.node_by_id(kp)
            if row is None or not node:
                continue
            gaps.append(Gap(
                knowledge_point=kp,
                error_type=entry["error_type"],
                repeat_count=entry["repeat_count"],
                days_since=entry["days_since"],
                cleared_count=entry["cleared_count"],
                items=node.get("items", []),
                completed_items=completed.get(row["id"], set()),
            ))
        activations = [
            parse_timestamp(row["reviewed_at"] or row["created_at"])
            for row in latest.values()
        ]
        start = max((at for at in activations if at is not None), default=utc_now()).astimezone().date()
        plan = build_path(gaps, max_per_day=self.cfg.max_practice_per_day, today=start)
        for block in plan:
            for item in block["items"]:
                item["mistake_id"] = latest[item["knowledge_point"]]["id"]
        return plan


def trace_markdown(sample_id: str, parsed: ParseResult, result: Dict[str, Any], trace: List[Dict[str, Any]]) -> str:
    lines = [
        f"# Trace · {sample_id} · {utc_now().isoformat(timespec='seconds')}",
        "",
        f"- provider: `{result['flags']['provider']}`",
        f"- input status: `{parsed.status}`",
        f"- grounding: score `{result['flags'].get('grounding_score')}` vs threshold `{result['flags'].get('threshold')}` "
        f"(unmapped={result['flags']['unmapped']})",
        f"- needs teacher review: `{result['flags']['needs_teacher_review']}`",
        f"- elapsed: `{result['flags']['elapsed_s']}s`",
        "",
        "## Input",
        "",
        "```json",
        json.dumps({"question": parsed.question, "steps": parsed.steps}, ensure_ascii=False, indent=2),
        "```",
        "",
        "## Output",
        "",
        "```json",
        json.dumps(result, ensure_ascii=False, indent=2),
        "```",
        "",
        "## Pipeline steps",
        "",
    ]
    for entry in trace:
        lines.append(f"- `{entry['step']}` @ {entry['at']}")
    lines.append("")
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Diagnose one wrong answer.")
    ap.add_argument("--sample", help="sample JSON from samples/ (question + steps already parsed)")
    ap.add_argument("--image", help="photo of the wrong answer (needs a vision provider)")
    ap.add_argument("--question", help="question text, skips photo parsing")
    ap.add_argument("--steps", help="student's working steps, separated by ';'")
    ap.add_argument("--student", default="anon")
    ap.add_argument("--json", action="store_true", help="print raw JSON only")
    ap.add_argument("--trace", action="store_true", help="save the run to traces/")
    ap.add_argument("--offline", action="store_true", help="force the offline rule baseline")
    ap.add_argument("--no-save", action="store_true", help="do not write to SQLite")
    args = ap.parse_args(argv)

    cfg = Config.from_env()
    pipe = Pipeline(cfg, force_offline=args.offline or not cfg.configured)

    if args.sample:
        with open(args.sample, "r", encoding="utf-8") as fh:
            sample = json.load(fh)
        parsed = from_sample(sample)
        sample_id = sample.get("id", os.path.basename(args.sample))
    elif args.image:
        parsed = parse_photo(pipe.provider, args.image)
        sample_id = os.path.basename(args.image)
    elif args.question or args.steps:
        parsed = from_text(args.question or "", [s.strip() for s in (args.steps or "").split(";") if s.strip()])
        sample_id = "cli"
    else:
        ap.error("provide --sample, --image, or --question/--steps")
        return 2

    result = pipe.run(parsed, student_id=args.student, image_path=args.image, save=not args.no_save)

    if args.trace:
        os.makedirs(os.path.join(ROOT, "traces"), exist_ok=True)
        path = os.path.join(ROOT, "traces", f"{utc_now().strftime('%Y%m%d_%H%M%S')}_{sample_id}.md")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(trace_markdown(sample_id, parsed, result, pipe.trace))
        result["trace_path"] = os.path.relpath(path, ROOT)

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"[{result['flags']['provider']}] {sample_id}")
        print(f"  status        : {result['status']}")
        print(f"  broken step   : {result['broken_step']}")
        print(f"  error type    : {result['error_type']}")
        print(f"  knowledge pt  : {result['knowledge_point']} (score {result['flags'].get('grounding_score')})")
        print(f"  confidence    : {result['confidence']}  review={result['flags']['needs_teacher_review']}")
        print(f"  practice      : {', '.join(result['practice']) or '—'}")
        print(f"  schedule      : {', '.join('day' + str(s['day']) for s in result['schedule']) or '—'}")
        if result.get("message"):
            print(f"  note          : {result['message']}")
        if result.get("trace_path"):
            print(f"  trace         : {result['trace_path']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
