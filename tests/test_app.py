"""Run the student workflow through Streamlit with an isolated database."""

import os
import sqlite3
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from streamlit.testing.v1 import AppTest


class TestApp(unittest.TestCase):
    def test_sample_submission_survives_reruns_without_an_extra_mistake(self):
        with TemporaryDirectory() as folder:
            db = Path(folder) / "app.db"
            with patch.dict(os.environ, {"DB_PATH": str(db), "PATHFINDER_OFFLINE": "1"}):
                app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "app.py"))
                app.run()
                self.assertIn("提交诊断", [button.label for button in app.button])
                next(button for button in app.button if button.label == "提交诊断").click().run()
                app.run()
                self.assertFalse(app.exception)
                with closing(sqlite3.connect(db)) as conn:
                    self.assertEqual(conn.execute("SELECT COUNT(*) FROM mistakes").fetchone()[0], 1)
                next(button for button in app.button if button.label == "复习路径").click().run()
                self.assertFalse(app.exception)
                self.assertEqual([button.label for button in app.button].count("已掌握"), 3)
