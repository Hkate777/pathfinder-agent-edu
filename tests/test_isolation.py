"""Data isolation gate: overlaps fail, freeze protects, missing hold-out is inert."""

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from scripts.check_data_isolation import run_gate


def _case(cid, question, steps):
    return {"id": cid, "question": question, "steps": steps}


class TestIsolationGate(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "samples").mkdir()
        (self.root / "evaluation").mkdir()
        (self.root / "knowledge_base").mkdir()
        self._patch = patch.multiple(
            "scripts.check_data_isolation",
            ROOT=self.root,
            HOLD_OUT=self.root / "evaluation" / "held_out.json",
            HOLD_OUT_HASH=self.root / "evaluation" / "held_out.hash",
            KB=self.root / "knowledge_base" / "knowledge_points.json",
        )
        self._patch.start()
        self.addCleanup(self._patch.stop)

    def _write(self, relpath: str, payload):
        path = self.root / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return path

    def test_missing_hold_out_is_inert(self):
        self._write("samples/a.json", [_case("d1", "Solve x - 4 = 10", ["x = 14"])])
        self.assertEqual(run_gate(), 0)

    def test_overlap_between_dev_and_hold_out_fails(self):
        self._write("samples/a.json", [_case("d1", "Solve x - 4 = 10", ["x = 14"])])
        self._write("evaluation/held_out.json",
                    {"cases": [_case("h1", "SOLVE X-4=10", ["x = 14"])]})
        self.assertEqual(run_gate(), 1, "normalised overlap must fail the gate")

    def test_kb_practice_leakage_fails(self):
        self._write("samples/a.json", [_case("d1", "unique dev question", ["s"])])
        self._write("knowledge_base/knowledge_points.json",
                    {"nodes": [{"id": "ALG-7.3", "items": [
                        {"id": "ALG-7.3 #1", "question": "Expand 5(y-2)"}]}]})
        self._write("evaluation/held_out.json",
                    {"cases": [_case("h1", "Expand  5(y-2)", ["10y - 4"])]})
        self.assertEqual(run_gate(), 1, "KB practice text must not appear in hold-out")

    def test_clean_setup_passes(self):
        self._write("samples/a.json", [_case("d1", "Solve x - 4 = 10", ["x = 14"])])
        self._write("knowledge_base/knowledge_points.json",
                    {"nodes": [{"id": "K", "items": [{"id": "K #1", "question": "Expand 5(y-2)"}]}]})
        self._write("evaluation/held_out.json",
                    {"cases": [_case("h1", "Different question entirely", ["s1"])]})
        self.assertEqual(run_gate(), 0)

    def test_freeze_detects_post_freeze_edits(self):
        self._write("samples/a.json", [])
        held = self._write("evaluation/held_out.json", {"cases": [_case("h1", "q1", ["s"])]})
        self.assertEqual(run_gate(freeze=True), 0)
        self.assertTrue((self.root / "evaluation" / "held_out.hash").exists())
        # Tamper after freeze
        held.write_text(json.dumps({"cases": [_case("h1", "EDITED", [])]}, ensure_ascii=False),
                        encoding="utf-8")
        self.assertEqual(run_gate(), 1, "post-freeze edits must fail")


if __name__ == "__main__":
    unittest.main()
