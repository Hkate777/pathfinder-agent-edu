"""CAS verifier: deterministic step verdicts must match teacher labels and
never guess where SymPy cannot decide."""

import json
import unittest
from pathlib import Path

from verifier import normalize, verify_work

ROOT = Path(__file__).resolve().parents[1]


def _first_fail(question, steps):
    return verify_work(question, steps).first_fail


class TestNormalize(unittest.TestCase):
    def test_unicode_and_prose_are_normalised(self):
        self.assertEqual(normalize("Solve x − 4 = 10"), "x - 4 = 10")
        self.assertEqual(normalize("2 × 3 ÷ 6"), "2 * 3 / 6")
        # NFKC folds fullwidth forms in place; only prose words are dropped.
        self.assertEqual(normalize("求 x：2（x+3）＝4x-6"), "x:2(x+3)=4x-6")
        self.assertNotIn("Solve", normalize("Solve x - 4 = 10"))


class TestEquationChains(unittest.TestCase):
    def test_wrong_chain_fails_at_teacher_labeled_breakpoint(self):
        self.assertEqual(
            _first_fail("Find the value of x: 2(x+3) = 4x - 6",
                        ["2x + 3 = 4x - 6", "3 = 2x - 6", "x = 4.5"]),
            0,
        )

    def test_correct_chain_passes_everywhere(self):
        self.assertIsNone(
            _first_fail("Find the value of x: 2(x+3) = 4x - 6",
                        ["2x + 6 = 4x - 6", "x = 6"]),
        )

    def test_sign_drop_is_caught_even_without_question_equation_fallback(self):
        self.assertEqual(_first_fail("Solve x - 4 = 10", ["x = 10 - 4", "x = 6"]), 0)

    def test_transpose_sign_variant(self):
        # equivalent-but-different route: 10 + 4 on the right is correct
        self.assertIsNone(_first_fail("Solve x - 4 = 10", ["x = 10 + 4", "x = 14"]))

    def test_implicit_multiplication_and_fractions(self):
        self.assertEqual(_first_fail("Add 1/2 + 1/3", ["1/2 + 1/3 = 2/5"]), 0)
        self.assertIsNone(_first_fail("Add 1/2 + 1/3", ["1/2 + 1/3 = 5/6"]))


class TestIdentityChecks(unittest.TestCase):
    def test_expansion_error_is_a_fail(self):
        self.assertEqual(_first_fail("Expand (a+b)^2", ["(a+b)^2 = a^2 + b^2"]), 0)

    def test_correct_expansion_passes(self):
        self.assertIsNone(_first_fail("Expand (a+b)^2", ["(a+b)^2 = a^2 + 2ab + b^2"]))


class TestHonestUnknowns(unittest.TestCase):
    def test_chemistry_is_unknown_not_failed(self):
        w = verify_work("Balance: H2 + O2 -> H2O", ["H2 + O2 -> H2O"])
        self.assertEqual(w.verdicts[0].verdict, "unknown")

    def test_inequality_is_unknown(self):
        w = verify_work("Solve x < 5", ["x = 3"])
        self.assertEqual(w.verdicts[0].verdict, "unknown")

    def test_unparseable_step_is_unknown(self):
        w = verify_work("Solve 2(x+3)=4x-6", ["……（字迹不清）"])
        self.assertEqual(w.verdicts[0].verdict, "unknown")

    def test_multi_variable_step_is_unknown(self):
        w = verify_work("If 3a + 2b = 12, find a when b = 3", ["a = 2"])
        self.assertEqual(w.verdicts[0].verdict, "unknown")


class TestTeacherLabelAgreement(unittest.TestCase):
    """The CAS must reproduce every *algebra* teacher label; outside coverage
    (chemistry, physics-with-units) it must stay honest (unknown/none)."""

    def test_dev_sample_agreement(self):
        results = {}
        for path in sorted((ROOT / "samples").glob("*.json")):
            s = json.loads(path.read_text(encoding="utf-8"))
            expected = s.get("expected", {})
            if s.get("parse_status") not in (None, "ok"):
                continue
            w = verify_work(s.get("question", ""), s.get("steps", []))
            ref = expected.get("broken_step_contains") or ""
            exp_idx = next((i for i, st in enumerate(s.get("steps", []))
                            if ref and ref in st), None)
            results[s["id"]] = (w.first_fail, exp_idx)

        for sid in ("alg_distributive", "alg_fraction_add", "alg_sign_drop", "alg_square_of_sum"):
            self.assertEqual(results[sid][0], results[sid][1], f"{sid} breakpoint mismatch")


if __name__ == "__main__":
    unittest.main()
