import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_script():
    path = ROOT / "scripts" / "jev_backtest.py"
    spec = importlib.util.spec_from_file_location("test_jev_backtest_script", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


BACKTEST = load_script()


LANES = '''
[lanes.codex-sol-medium]
engine = "codex"
model = "gpt-5.6-sol"
engine_args = ["-c", "model_reasoning_effort=medium"]
criteria = "normal"
evidence = "test"

[lanes.codex-astra]
engine = "codex"
model = "gpt-6-astra"
engine_args = []
criteria = "hard"
evidence = "test"

[lanes.claude-sonnet]
engine = "claude"
model = "sonnet"
engine_args = []
criteria = "writing"
evidence = "test"
'''


class JevBacktestTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.log = self.root / "runs.jsonl"
        self.runs = self.root / "runs"
        self.runs.mkdir()
        self.lanes = self.root / "lanes.toml"
        self.lanes.write_text(LANES, encoding="utf-8")
        self.cache = self.root / "cache.jsonl"
        self.client = self.root / "client.py"
        self.marker = self.root / "calls.jsonl"
        self._write_client(False)
        self._make_fixture()

    def tearDown(self):
        self.temp.cleanup()

    def _write_client(self, raises):
        if raises:
            body = "def ask(*args, **kwargs):\n    raise AssertionError('cache miss')\n"
        else:
            body = f'''import json
from pathlib import Path
MARKER = Path({str(self.marker)!r})
PICKS = {{"alpha": "codex-sol-medium", "beta": "claude-sonnet"}}
def ask(source_class, state, questions, caller="unknown"):
    with MARKER.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({{"source_class": source_class, "caller": caller}}) + "\\n")
    return {{"lane": {{"choice": PICKS[state["task_type"]], "confidence": 0.8}}}}
'''
        self.client.write_text(body, encoding="utf-8")

    def _make_fixture(self):
        # alpha has a three-task medium baseline; a fourth alpha task ran astra
        # and is deliberately picked medium, making its estimate known.
        fixtures = [
            ("r1", "a1", "alpha", "codex", "gpt-5.6-sol", "medium", "PASS", 100, None),
            ("r2", "a2", "alpha", "codex", "gpt-5.6-sol", "medium", "FAIL", 200, ("PASS", 50)),
            ("r3", "a3", "alpha", "codex", "gpt-5.6-sol", "medium", "PASS", 300, None),
            ("r4", "a4", "alpha", "codex", "gpt-6-astra", None, "PASS", 400, ("PASS", 100)),
            ("r5", "b1", "beta", "claude", "claude-sonnet-5", None, "PASS", 10, None),
            ("r6", "b2", "beta", "claude", "claude-sonnet-5-5", None, "FAIL", 20, None),
            ("r7", "b3", "beta", "weird", "mystery", None, "PASS", 30, None),
        ]
        rows = []
        for index, (run_id, key, kind, engine, model, effort, verdict, tokens, retry) in enumerate(fixtures):
            task = {"key": key, "spec": f"do {key}", "check": "true", "engine": engine}
            (self.runs / f"{run_id}.json").write_text(json.dumps({"tasks": [task]}), encoding="utf-8")
            first = {"run_id": run_id, "task_key": key, "worker_engine": engine, "model": model,
                     "reasoning_effort": effort, "task_type": kind, "verdict": verdict, "retry": 0,
                     "worker_tokens": tokens, "duration_ms": 1,
                     "logged_at": f"2026-01-{index + 1:02d}T00:00:00Z", "notes": ""}
            rows.append(first)
            if retry:
                retry_verdict, retry_tokens = retry
                rows.append({**first, "verdict": retry_verdict, "worker_tokens": retry_tokens, "retry": 1,
                             "logged_at": f"2026-01-{index + 1:02d}T00:01:00Z"})
        self.log.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")

    def run_backtest(self, limit=None):
        return BACKTEST.backtest(self.log, self.runs, self.lanes, self.client, self.cache, limit=limit)

    def test_mapping_estimates_cache_and_rendering(self):
        data = self.run_backtest()
        self.assertEqual(7, data["counts"]["tasks_seen"])
        self.assertEqual(7, data["counts"]["answered"])
        self.assertEqual(7, data["counts"]["cached"])
        self.assertEqual(5, data["total"]["agreements"])
        self.assertEqual(1, data["total"]["unknown_thin"])

        confusion = {(row["actual_lane"], row["jev_pick"]): row["tasks"] for row in data["confusion"]}
        self.assertEqual(2, confusion[("claude-sonnet", "claude-sonnet")])
        self.assertEqual(1, confusion[("other:weird/mystery/-", "claude-sonnet")])

        disagreements = {row["key"]: row for row in data["disagreements"]}
        known = disagreements["a4"]
        self.assertAlmostEqual(2 / 3, known["estimated_first_try"])
        self.assertEqual(200, known["estimated_tokens"])
        self.assertEqual(-300, known["token_delta"])  # 200 estimate - (400 + 100) real job
        self.assertIsNone(disagreements["b3"]["estimated_tokens"])

        calls = [json.loads(line) for line in self.marker.read_text().splitlines()]
        self.assertTrue(all(row == {"source_class": "ringer-spec", "caller": "jev-backtest"} for row in calls))
        first_markdown = BACKTEST.render_markdown(data)
        self.assertIn("counterfactual estimate", first_markdown)

        call_count = len(self.marker.read_text().splitlines())
        self._write_client(True)
        second = self.run_backtest()
        self.assertEqual(call_count, len(self.marker.read_text().splitlines()))
        self.assertEqual(first_markdown, BACKTEST.render_markdown(second))

    def test_limit_is_after_logged_at_sort(self):
        data = self.run_backtest(limit=3)
        self.assertEqual(3, data["counts"]["tasks_seen"])
        self.assertEqual(3, data["counts"]["answered"])
        self.assertEqual(3, data["total"]["tasks"])

    def test_actual_lane_alias_and_other(self):
        lanes = BACKTEST.ringer.load_lanes(self.lanes)
        self.assertEqual("claude-sonnet", BACKTEST.actual_lane(
            {"worker_engine": "claude", "model": "sonnet", "reasoning_effort": None}, lanes))
        self.assertEqual("claude-sonnet", BACKTEST.actual_lane(
            {"worker_engine": "claude", "model": "claude-sonnet-5-5", "reasoning_effort": None}, lanes))
        self.assertEqual("other:codex/gpt-5.6-sol/high", BACKTEST.actual_lane(
            {"worker_engine": "codex", "model": "gpt-5.6-sol", "reasoning_effort": "high"}, lanes))


if __name__ == "__main__":
    unittest.main()
