"""Regressions for the four phase-0 UI blockers.

Each test drives the real Streamlit app with an isolated SQLite database and the
offline rule provider (no network, no API key). They assert observable behaviour
through AppTest, not implementation details.
"""

import os
import sqlite3
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

APP = str(Path(__file__).resolve().parents[1] / "app.py")


def _boot(db: Path):
    app = AppTest.from_file(APP)
    app.run()
    return app


def _count(db: Path) -> int:
    with closing(sqlite3.connect(db)) as conn:
        return conn.execute("SELECT COUNT(*) FROM mistakes").fetchone()[0]


class TestPhase0Blockers(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "app.db"
        self.env = patch.dict(os.environ, {"DB_PATH": str(self.db), "PATHFINDER_OFFLINE": "1"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_a_submit_button_survives_result_for_continuous_diagnosis(self):
        """P0-1: after one diagnosis the submit button stays reachable."""
        app = _boot(self.db)
        self.assertIn("提交诊断", [b.label for b in app.button])
        next(b for b in app.button if b.label == "提交诊断").click().run()
        self.assertFalse(app.exception)
        self.assertIn("诊断下一题", [b.label for b in app.button])
        self.assertIn("提交诊断", [b.label for b in app.button])

    def test_b_repeated_submission_is_idempotent(self):
        """P0-1: a second submission of the same input reuses the stored mistake."""
        app = _boot(self.db)
        # Submit, then clear only the result and submit the identical sample again.
        next(b for b in app.button if b.label == "提交诊断").click().run()
        self.assertEqual(_count(self.db), 1)
        if "diagnosis" in app.session_state:
            del app.session_state["diagnosis"]
        app.run()
        next(b for b in app.button if b.label == "提交诊断").click().run()
        self.assertFalse(app.exception)
        self.assertEqual(_count(self.db), 1, "repeated submission must not add a second row")

    def test_c_switching_students_clears_transcript_and_photo_widgets(self):
        """P0-2: changing student never leaks the previous student's photo state."""
        app = _boot(self.db)
        app.session_state["transcript"] = ("student-01", "digest", object())
        app.session_state["photo_upload_student-01"] = "uploaded"
        app.session_state["diagnosis"] = ("student-01", "sample", "x", {})

        switch = app.get_by_key("switch_student_student-01")
        sid_input = next(t for t in app.text_input if t.label == "学生编号")
        sid_input.set_value("student-02")
        # The form submitter is tracked under a synthetic key; use its button by label.
        next(b for b in app.button if b.label == "切换学生").click().run()

        self.assertFalse(app.exception)
        self.assertEqual(app.session_state.get("student_id"), "student-02")
        self.assertNotIn("transcript", app.session_state)
        self.assertNotIn("diagnosis", app.session_state)
        self.assertNotIn("photo_upload_student-01", app.session_state)

    def test_d_teacher_can_assign_kp_to_an_unmapped_mistake(self):
        """P0-4: assigning a KP from review unlocks practice and heatmap."""
        from store import (
            add_mistake, connect, effective_kp, init_db, save_kp_override,
            save_review, class_heatmap,
        )
        from diagnose import Pipeline
        from llm import Config

        # Create an unmapped mistake directly (simulates low grounding score).
        with closing(connect(str(self.db))) as conn:
            init_db(conn)
            mid = add_mistake(conn, {
                "student_id": "learner", "question_text": "custom question",
                "steps": ["step one"], "error_type": "conceptual",
                "knowledge_point_id": None, "grounded": 0, "confidence": 0.4,
            })
            self.assertIsNone(effective_kp(conn, mid))
            self.assertTrue(save_review(conn, mid, "confirmed", "Teacher verified."))
            self.assertTrue(save_kp_override(conn, mid, "ALG-7.3"))
            self.assertEqual(effective_kp(conn, mid), "ALG-7.3")
            self.assertTrue(any(row["knowledge_point"] == "ALG-7.3" for row in class_heatmap(conn)))

        cfg = Config(db_path=str(self.db))
        pipe = Pipeline(cfg, force_offline=True)
        with closing(connect(str(self.db))) as conn:
            plan = pipe.review_plan(conn, "learner")
        items = [i["item_id"] for block in plan for i in block["items"]]
        self.assertEqual(items, ["ALG-7.3 #1", "ALG-7.3 #2", "ALG-7.3 #3"],
                         "assigned KP must release exactly the curriculum practice")


if __name__ == "__main__":
    unittest.main()
