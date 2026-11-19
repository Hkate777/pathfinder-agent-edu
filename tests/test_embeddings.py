"""Local semantic grounding (sentence-transformers) — multilingual retrieval.

Skipped automatically when the optional dependency or model weights are absent,
so the offline CI path stays dependency-light.
"""

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

try:
    import sentence_transformers  # noqa: F401
    HAVE_ST = True
except Exception:  # noqa: BLE001
    HAVE_ST = False


@unittest.skipUnless(HAVE_ST, "sentence-transformers not installed")
class TestSemanticGrounding(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from grounding import Grounder, load_kb
        from llm import Config, SentenceTransformerEmbedder

        cfg = Config.from_env()
        try:
            embedder = SentenceTransformerEmbedder(cfg)
            cls.probe = embedder.embed(["grounding probe"])[0]
        except Exception as exc:  # noqa: BLE001 - missing weights / offline box
            raise unittest.SkipTest(f"semantic model unavailable: {type(exc).__name__}")

        nodes, matchers = load_kb(
            str(ROOT / "knowledge_base" / "knowledge_points.json"),
            str(ROOT / "knowledge_base" / "matchers.json"),
        )

        class Provider:
            name = "st-local"
            embedding_kind = "local-st"
            embed = staticmethod(embedder.embed)

        cls.grounder = Grounder(nodes, matchers, Provider(), threshold=0.42)

    def test_output_is_384_dimensional_and_normalised(self):
        self.assertEqual(len(self.probe), 384)
        norm = sum(v * v for v in self.probe) ** 0.5
        self.assertAlmostEqual(norm, 1.0, places=3)

    def test_chinese_questions_ground_where_hashing_cannot(self):
        cases = [
            ("求 x：2(x+3)=4x-6", "ALG-7.3"),
            ("解方程 x 减 4 等于 10，移项变号", "ALG-7.2"),
            ("二分之一加三分之一等于多少", "ALG-7.4"),
            ("完全平方公式展开 (a+b) 的平方", "ALG-8.3"),
            ("化学方程式配平 H2 + O2 生成水", "CHE-8.1"),
            ("牛顿第二定律 力除以质量求加速度", "PHY-9.1"),
        ]
        for question, expected in cases:
            with self.subTest(question=question):
                self.assertEqual(self.grounder.ground(question).knowledge_point, expected)

    def test_negative_controls_stay_unmapped(self):
        for question in ("今天天气怎么样", "The capital of France is Paris"):
            with self.subTest(question=question):
                self.assertTrue(self.grounder.ground(question).unmapped)

    def test_english_dev_questions_still_ground(self):
        self.assertEqual(self.grounder.ground(
            "Find the value of x: 2(x+3) = 4x - 6 ; 2x + 3 = 4x - 6 ; x = 4.5"
        ).knowledge_point, "ALG-7.3")
        self.assertEqual(self.grounder.ground(
            "A 500 g mass is pushed with 10 N ; a = F/m = 10/500 ; a = 0.02 m/s^2"
        ).knowledge_point, "PHY-9.2")


if __name__ == "__main__":
    unittest.main()
