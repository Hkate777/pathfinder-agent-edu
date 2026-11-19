"""Regressions spanning model output, diagnosis, and durable review state."""

from pathlib import Path
from datetime import date, datetime, timedelta, timezone
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from diagnose import Pipeline
from llm import Config, LLMResponseError, StubProvider, _tolerant_json
from store import (
    add_mistake, connect, effective_kp, gap_profile, init_db, record_practice,
    register_student, review_cases, save_kp_override, save_review,
)
from vision import from_text, parse_photo


class UncertainProvider(StubProvider):
    def diagnose(self, question, steps):
        return {**super().diagnose(question, steps), "broken_step": "the student never wrote this"}

    def parse_photo(self, image_path):
        return {"status": "ok", "question": "", "steps": []}


class TestWorkflows(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = Config(db_path=str(Path(self.tmp.name) / "workflows.db"))
        self.pipe = Pipeline(self.cfg, force_offline=True)
        self.parsed = from_text("Solve 2(x+3) = 4x - 6", ["2x + 3 = 4x - 6", "x = 4.5"])

    def test_top_level_json_must_be_an_object(self):
        with self.assertRaises(LLMResponseError):
            _tolerant_json('[{"status": "ok"}]')

    def test_api_key_without_endpoint_does_not_enable_online_mode(self):
        with patch("llm.dotenv_values", return_value={}), patch.dict(
            "os.environ", {"LLM_API_KEY": "fixture-key", "TEXT_MODEL": "fixture-model"}, clear=True,
        ):
            self.assertFalse(Config.from_env().configured)

    def test_empty_photo_transcript_abstains(self):
        self.assertFalse(parse_photo(UncertainProvider(self.cfg), "unused.png").ok)

    def test_unquoted_step_requires_review_and_blocks_practice(self):
        pipe = Pipeline(self.cfg, provider=UncertainProvider(self.cfg))
        result = pipe.run(self.parsed, student_id="learner")
        self.assertIsNone(result["broken_step"])
        self.assertTrue(result["flags"]["needs_teacher_review"])
        self.assertTrue(all(not day["items"] for day in result["plan"]))

    def test_correct_expansion_does_not_trigger_the_dropped_factor_rule(self):
        correct = from_text(self.parsed.question, ["2x + 6 = 4x - 6", "x = 6"])
        result = self.pipe.run(correct, save=False)
        self.assertIsNone(result["broken_step"])
        self.assertTrue(result["flags"]["needs_teacher_review"])

    def test_correct_fraction_sum_does_not_trigger_the_same_denominator_rule(self):
        result = self.pipe.run(from_text("Compute 1/2 + 1/3", ["1/2 + 1/3 = 5/6"]), save=False)
        self.assertIsNone(result["broken_step"])
        self.assertTrue(result["flags"]["needs_teacher_review"])

    def test_rule_baseline_reports_the_first_matching_step(self):
        steps = ["1/2 + 1/3 = 2/5", "(a+b)^2 = a^2 + b^2"]
        result = self.pipe.run(from_text("Review both steps", steps), save=False)
        self.assertEqual(result["broken_step"], "1/2 + 1/3 = 2/5")

    def test_review_queue_resolves_without_rewriting_diagnosis(self):
        pipe = Pipeline(self.cfg, provider=UncertainProvider(self.cfg))
        result = pipe.run(self.parsed, student_id="learner")
        with connect(self.cfg.db_path) as conn:
            self.addCleanup(conn.close)
            register_student(conn, "learner", "Form 1")
            self.assertEqual([row["id"] for row in review_cases(conn, .6, "Form 1")], [result["mistake_id"]])
            self.assertEqual(review_cases(conn, .6, "Form 2"), [])
            self.assertTrue(save_review(conn, result["mistake_id"], "rejected", "Check original working."))
            self.assertFalse(save_review(conn, result["mistake_id"], "confirmed", "Duplicate action."))
            self.assertEqual(review_cases(conn, .6, "Form 1"), [])
            self.assertIsNone(conn.execute("SELECT broken_step FROM mistakes").fetchone()[0])

    def test_review_approval_releases_only_existing_curriculum_practice(self):
        pipe = Pipeline(self.cfg, provider=UncertainProvider(self.cfg))
        result = pipe.run(self.parsed, student_id="learner")
        with connect(self.cfg.db_path) as conn:
            self.addCleanup(conn.close)
            self.assertTrue(save_review(conn, result["mistake_id"], "confirmed", "Verified the original work."))
            plan = pipe.review_plan(conn, "learner")
            self.assertEqual(
                [item["item_id"] for day in plan for item in day["items"]],
                ["ALG-7.3 #1", "ALG-7.3 #2", "ALG-7.3 #3"],
            )
            self.assertIn("2x + 6", plan[0]["items"][0]["solution"])

    def test_repeat_practice_pass_does_not_inflate_mastery(self):
        result = self.pipe.run(self.parsed, student_id="learner")
        with connect(self.cfg.db_path) as conn:
            self.addCleanup(conn.close)
            record_practice(conn, result["mistake_id"], "ALG-7.3 #1", 1, "pass")
            record_practice(conn, result["mistake_id"], "ALG-7.3 #1", 1, "pass")
            self.assertEqual(gap_profile(conn, "learner")[0]["cleared_count"], 1)

    def test_completed_item_stays_out_of_active_path(self):
        result = self.pipe.run(self.parsed, student_id="learner")
        with connect(self.cfg.db_path) as conn:
            self.addCleanup(conn.close)
            record_practice(conn, result["mistake_id"], "ALG-7.3 #1", 1, "pass")
            plan = self.pipe.review_plan(conn, "learner")
            self.assertEqual([(block["day"], [i["item_id"] for i in block["items"]]) for block in plan],
                             [(1, []), (3, ["ALG-7.3 #2"]), (7, ["ALG-7.3 #3"])])

    def test_legacy_quote_without_matching_step_still_requires_review(self):
        with connect(self.cfg.db_path) as conn:
            self.addCleanup(conn.close)
            init_db(conn)
            mid = add_mistake(conn, {
                "student_id": "learner", "steps": ["2x + 3 = 4x - 6"],
                "broken_step": "2x + 3", "error_type": "conceptual",
                "grounded": True, "knowledge_point_id": "ALG-7.3", "confidence": 0.9,
            })
            self.assertEqual([row["id"] for row in review_cases(conn, .6)], [mid])
            self.assertTrue(all(not block["items"] for block in self.pipe.review_plan(conn, "learner")))

    def test_plan_remembers_the_diagnosis_date(self):
        with connect(self.cfg.db_path) as conn:
            self.addCleanup(conn.close)
            init_db(conn)
            add_mistake(conn, {
                "student_id": "learner", "steps": ["2x + 3 = 4x - 6"], "broken_step": "2x + 3 = 4x - 6",
                "error_type": "conceptual", "grounded": True, "knowledge_point_id": "ALG-7.3",
                "confidence": 0.9, "created_at": datetime(2025, 1, 2, 12).astimezone(timezone.utc).isoformat(),
            })
            self.assertEqual([day["date"] for day in self.pipe.review_plan(conn, "learner")],
                             ["2025-01-02", "2025-01-04", "2025-01-08"])

    def test_daily_practice_limit_counts_distinct_items_even_after_passing(self):
        result = self.pipe.run(self.parsed, student_id="learner")
        mid = result["mistake_id"]
        with connect(self.cfg.db_path) as conn:
            self.addCleanup(conn.close)
            self.assertFalse(record_practice(conn, mid, "ALG-7.3 #1", 1, "pass",
                                             not_before=date.today() + timedelta(days=1)))
            self.assertTrue(record_practice(conn, mid, "ALG-7.3 #1", 1, "retry", max_per_day=1))
            self.assertTrue(record_practice(conn, mid, "ALG-7.3 #1", 1, "pass", max_per_day=1))
            self.assertFalse(record_practice(conn, mid, "ALG-7.3 #2", 3, "pass", max_per_day=1))

    def test_submission_key_is_idempotent_across_pipeline_runs(self):
        first = self.pipe.run(self.parsed, student_id="learner", submission_key="sub-1")
        second = self.pipe.run(self.parsed, student_id="learner", submission_key="sub-1")
        self.assertEqual(first["mistake_id"], second["mistake_id"])
        self.assertFalse(first["flags"]["duplicate"])
        self.assertTrue(second["flags"]["duplicate"])
        with connect(self.cfg.db_path) as conn:
            self.addCleanup(conn.close)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM mistakes").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM submissions").fetchone()[0], 1)

    def test_teacher_kp_override_is_idempotent_and_visible_in_profile(self):
        result = self.pipe.run(self.parsed, student_id="learner")
        mid = result["mistake_id"]
        with connect(self.cfg.db_path) as conn:
            self.addCleanup(conn.close)
            self.assertTrue(save_review(conn, mid, "confirmed", "Verified."))
            self.assertTrue(save_kp_override(conn, mid, "ALG-8.3"))
            # Re-asserting a different KP updates in place (no duplicate rows).
            self.assertTrue(save_kp_override(conn, mid, "ALG-7.3"))
            self.assertEqual(effective_kp(conn, mid), "ALG-7.3")
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM teacher_kp_override").fetchone()[0], 1)
            self.assertEqual(gap_profile(conn, "learner")[0]["knowledge_point"], "ALG-7.3")
