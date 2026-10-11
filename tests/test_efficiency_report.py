#!/usr/bin/env python3
"""Tests for the zero-model efficiency report generator."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "efficiency_report.py"


def load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("efficiency_report", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class EfficiencyReportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.log = root / "runs.jsonl"
        self.runs_dir = root / "runs"
        self.out = root / "out"
        self.runs_dir.mkdir()
        rows = [
            self.row("run-1", "a", "FAIL", 100, "2026-01-01T00:00:00+00:00",
                     notes="raw_check_output_first_2000_chars:\nassertion failed"),
            self.row("run-1", "a", "PASS", 50, "2026-01-01T00:01:00+00:00", retry=True),
            self.row("run-1", "b", "FAIL", 200, "2026-01-01T00:02:00+00:00",
                     notes="raw_check_output_first_2000_chars:\n[ringer] missing expected files: x.txt"),
            self.row("run-1", "b", "ERROR", 25, "2026-01-01T00:03:00+00:00", retry=True,
                     notes="worker_error=command not found\nraw_check_output_first_2000_chars:\nsetup failed"),
            self.row("run-2", "c", "TIMEOUT", 300, "2026-01-02T00:00:00+00:00", model="",
                     notes="raw_check_output_first_2000_chars:\n[ringer.py] check timed out after 60s"),
            self.row("run-2", "d", "PASS", 40, "2026-01-02T00:01:00+00:00",
                     engine="claude", model="haiku", effort=None, task_type="docs"),
            self.row("run-2", "e", "PASS", 80, "2026-01-02T00:02:00+00:00"),
            self.row("run-2", "f", "PASS", 120, "2026-01-02T00:03:00+00:00"),
            self.row("run-2", "mocked", "FAIL", 9999, "2026-01-02T00:04:00+00:00",
                     engine="mock", model="mock-model"),
        ]
        self.log.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
        self.write_state("run-1", {"a": "a" * 1000, "b": "b" * 4000})
        self.write_state("run-2", {"c": "c" * 7000, "d": "d" * 9000,
                                   "e": "e" * 2999, "f": "f" * 6000})
        self.module = load_module()

    @staticmethod
    def row(
        run_id: str, key: str, verdict: str, tokens: int, logged_at: str, *,
        retry: bool = False, notes: str = "raw_check_output_first_2000_chars:\n",
        engine: str = "codex", model: str = "gpt-5.6-sol", effort: str | None = "medium",
        task_type: str = "code",
    ) -> dict[str, object]:
        return {"run_id": run_id, "task_key": key, "worker_engine": engine,
                "model": model, "reasoning_effort": effort, "task_type": task_type,
                "verdict": verdict, "retry": retry, "worker_tokens": tokens,
                "duration_ms": tokens * 10, "logged_at": logged_at, "notes": notes,
                "spec": "truncated"}

    def write_state(self, run_id: str, specs: dict[str, str]) -> None:
        state = {"run_id": run_id, "state": "done",
                 "tasks": [{"key": key, "spec": spec, "engine": "codex", "status": "done",
                            "attempts": 1, "setup_error": None, "check_timed_out": False,
                            "timeout_s": 60, "tokens": 1} for key, spec in specs.items()]}
        (self.runs_dir / f"{run_id}.json").write_text(json.dumps(state), encoding="utf-8")

    def run_cli(self, *extra: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCRIPT), "--log", str(self.log),
             "--runs-dir", str(self.runs_dir), "--out", str(self.out), *extra],
            cwd=ROOT, text=True, capture_output=True, check=False,
        )

    def test_python_api_totals_buckets_baseline_and_determinism(self) -> None:
        data = self.module.generate_reports(self.log, self.runs_dir, self.out)

        self.assertEqual({"rows": 9, "runs": 2, "tasks": 6,
                          "total_worker_tokens": 915, "total_wall_seconds": 9.15,
                          "tokens_on_non_pass_attempts": 625,
                          "wall_seconds_on_non_pass_attempts": 6.25}, data["totals"])
        counts = {item["bucket"]: item["attempts"] for item in data["waste_buckets"]}
        self.assertEqual([2, 3, 3, 2, 1, 1, 1, 1, 1], list(counts.values()))
        self.assertNotIn("mock", (self.out / "baseline.md").read_text(encoding="utf-8"))
        baseline = (self.out / "baseline.md").read_text(encoding="utf-8")
        self.assertIn("50% (2/4)", baseline)
        self.assertIn("docs | claude | haiku | - | 1 | 100% (1/1)", baseline)
        self.assertIn("| THIN |", baseline)

        first = {path.name: path.read_bytes() for path in self.out.iterdir()}
        self.module.generate_reports(self.log, self.runs_dir, self.out)
        second = {path.name: path.read_bytes() for path in self.out.iterdir()}
        self.assertEqual(first, second)

    def test_cli_writes_paths_and_json_has_all_sections(self) -> None:
        result = self.run_cli()
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual([str(self.out / "waste-audit.md"), str(self.out / "baseline.md")],
                         result.stdout.splitlines())

        json_result = self.run_cli("--json")
        self.assertEqual(0, json_result.returncode, json_result.stderr)
        data = json.loads(json_result.stdout)
        self.assertEqual(9, data["totals"]["rows"])
        self.assertEqual(
            {"totals", "waste_buckets", "broken_check_signatures",
             "runs_by_wasted_tokens", "spec_size_vs_first_try_pass",
             "missing_run_json_tasks", "baseline", "date_range"},
            set(data),
        )

    def test_missing_log_exits_two(self) -> None:
        self.log.unlink()
        result = self.run_cli()
        self.assertEqual(2, result.returncode)
        self.assertIn("log not found", result.stderr)


if __name__ == "__main__":
    unittest.main()
